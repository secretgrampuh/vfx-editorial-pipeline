"""Mirror a CG or tracking vendor's review activity - Version status, a
Task-status cascade, and review notes with attachments - from the
vendor's own ShotGrid site onto the studio's, for one review playlist.

This is a level up from shotgrid_status_mirror.py: that tool mirrors
Task/Version status fields between the two sites; this one mirrors the
actual review conversation. For the CG vendor, a client-approved Version
gets marked approved on the studio side and cascades the shot's Lighting
task to finished; a Version with open notes but no approval gets marked
"needs touch-up" and cascades Lighting to in-progress. For a tracking
vendor, status is mirrored straight across (their status codes were
already the studio's own convention). Either way, every open note on the
vendor's Version - reformatted from the vendor's own "{version} - note
text" convention, with any attachment the reviewer included - gets
copied onto the matching studio Version, skipping notes that are already
there so re-running this is safe.

Playlist naming: the vendor-side playlist is assumed to be the studio's
own playlist name with the vendor's name prefixed (matching the
convention conductor_downloader.py and roto_forwarder.py already
use), rather than the original tool's date-string matching against both
sites' full playlist lists with an interactive fallback prompt when it
guessed wrong.
"""

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path

import shotgun_api3
import yaml

LOGGER = logging.getLogger("vendor_note_sync")

STATUS_ON_APPROVAL = ("apr", "fin")
STATUS_ON_NOTES = ("nts", "ip")


@dataclass
class NoteSyncConfig:
    studio_server: str
    studio_script_name: str
    studio_script_key: str
    studio_project_id: int
    vendor_server: str
    vendor_script_name: str
    vendor_script_key: str
    vendor_project_id: int
    cg_vendor_name: str
    lighting_task_content: str
    cc_group_id: int
    default_addressee_id: int
    notes_attachment_root: Path
    max_note_length: int
    vendor_status_skip: str

    @classmethod
    def from_yaml(cls, config_path):
        with open(config_path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        studio = raw["shotgrid_studio"]
        vendor = raw["shotgrid_cg_vendor"]
        vendors = raw["vendors"]
        section = raw["vendor_note_sync"]
        return cls(
            studio_server=studio["server"],
            studio_script_name=studio["script_name"],
            studio_script_key=studio["script_key"],
            studio_project_id=int(studio["project_id"]),
            vendor_server=vendor["server"],
            vendor_script_name=vendor["script_name"],
            vendor_script_key=vendor["script_key"],
            vendor_project_id=int(vendor["project_id"]),
            cg_vendor_name=vendors["cg_vendor_name"],
            lighting_task_content=section["lighting_task_content"],
            cc_group_id=int(section["cc_group_id"]),
            default_addressee_id=int(section["default_addressee_id"]),
            notes_attachment_root=Path(section["notes_attachment_root"]),
            max_note_length=int(section["max_note_length"]),
            vendor_status_skip=section["vendor_status_skip"],
        )

    def studio_client(self):
        return shotgun_api3.Shotgun(self.studio_server, self.studio_script_name, self.studio_script_key)

    def vendor_client(self):
        return shotgun_api3.Shotgun(self.vendor_server, self.vendor_script_name, self.vendor_script_key)


def find_playlist_versions(sg_client, project_id, playlist_name, fields):
    versions = sg_client.find("Version", [["project", "is", {"type": "Project", "id": project_id}]], fields)
    return [v for v in versions if v["playlists"] and v["playlists"][0]["name"] == playlist_name]


def build_studio_version_lookup(sg_client, project_id, playlist_name):
    fields = ["id", "code", "entity", "playlists"]
    lookup = {}
    for version in find_playlist_versions(sg_client, project_id, playlist_name, fields):
        shot_id = version["entity"]["id"] if version.get("entity") else None
        lookup[version["code"]] = {"id": version["id"], "shot_id": shot_id}
    return lookup


def resolve_studio_version(lookup, vendor_version_code):
    """Vendor version codes sometimes carry a sub-version suffix (e.g. a
    trailing ".01") the studio side's code doesn't have - try an exact
    match first, then the code with everything after the last dot
    stripped."""
    if vendor_version_code in lookup:
        return lookup[vendor_version_code]
    return lookup.get(vendor_version_code.split(".")[0])


def update_lighting_task(sg_client, project_id, shot_id, status, config):
    tasks = sg_client.find(
        "Task",
        [
            ["project", "is", {"type": "Project", "id": project_id}],
            ["entity.Shot.id", "is", shot_id],
            ["content", "is", config.lighting_task_content],
        ],
        ["id"],
    )
    for task in tasks:
        sg_client.update("Task", task["id"], {"sg_status_list": status})


def sync_cg_vendor_status(studio_client, studio_lookup, vendor_version, config):
    target = resolve_studio_version(studio_lookup, vendor_version["code"])
    if target is None:
        return f"{vendor_version['code']} does not exist in the studio playlist"

    if vendor_version["client_approved"]:
        version_status, task_status = STATUS_ON_APPROVAL
    elif vendor_version["open_notes"]:
        version_status, task_status = STATUS_ON_NOTES
    else:
        return None

    studio_client.update("Version", target["id"], {"sg_status_list": version_status})
    if target["shot_id"] is not None:
        update_lighting_task(studio_client, config.studio_project_id, target["shot_id"], task_status, config)
    return None


def sync_tracking_vendor_status(studio_client, studio_lookup, vendor_version, config):
    target = resolve_studio_version(studio_lookup, vendor_version["code"])
    if target is None:
        return f"{vendor_version['code']} does not exist in the studio playlist"
    status = vendor_version["sg_status_list"]
    if status == config.vendor_status_skip:
        status = ""
    studio_client.update("Version", target["id"], {"sg_status_list": status})
    return None


def parse_note_comment(note_text, vendor_version_code):
    """The vendor's own note-text convention is "{version} - {comment}",
    with a "[DATE INITIALS]" tag rebuilt from the note's own first two
    space-separated tokens. Falls back to the raw text if it doesn't
    match that convention."""
    marker = f"{vendor_version_code} - "
    if marker not in note_text:
        return note_text
    tokens = note_text.split(" ")
    body = note_text.split(marker, 1)[1]
    return f"[{tokens[0]} {tokens[1]}] {body}"


def download_note_attachments(vendor_client, attachments, destination_root):
    paths = []
    if not attachments:
        return paths
    destination_root.mkdir(parents=True, exist_ok=True)
    for attachment in attachments:
        local_path = destination_root / attachment["name"]
        vendor_client.download_attachment(attachment, str(local_path))
        paths.append(local_path)
    return paths


def create_studio_note(studio_client, content, version_id, shot_id, config):
    note_links = [{"id": version_id, "type": "Version"}]
    if shot_id is not None:
        note_links.append({"id": shot_id, "type": "Shot"})
    data = {
        "sg_status_list": "ip",
        "content": content,
        "note_links": note_links,
        "addressings_cc": [{"id": config.cc_group_id, "type": "Group"}],
        "addressings_to": [{"id": config.default_addressee_id, "type": "HumanUser"}],
        "project": {"type": "Project", "id": config.studio_project_id},
    }
    return studio_client.create("Note", data)


def sync_notes(studio_client, vendor_client, studio_lookup, vendor_version, config):
    messages = []
    target = resolve_studio_version(studio_lookup, vendor_version["code"])
    if target is None:
        return [f"{vendor_version['code']} does not exist in the studio playlist (notes not synced)"]
    if not vendor_version["open_notes"]:
        return messages

    studio_version = studio_client.find_one("Version", [["id", "is", target["id"]]], ["id", "code", "open_notes"])

    for note in vendor_version["open_notes"]:
        note_detail = vendor_client.find_one("Note", [["id", "is", note["id"]]], ["id", "attachments"])
        comment_body = parse_note_comment(note["name"], vendor_version["code"])
        if len(comment_body) > config.max_note_length:
            messages.append(f"Comment for {vendor_version['code']} may be too long: {comment_body}")

        if any(comment_body in existing["name"] for existing in studio_version["open_notes"]):
            LOGGER.info("Note already synced on %s, skipping", vendor_version["code"])
            continue

        attachment_paths = download_note_attachments(vendor_client, note_detail["attachments"], config.notes_attachment_root)
        created_note = create_studio_note(studio_client, comment_body, target["id"], target["shot_id"], config)
        for attachment_path in attachment_paths:
            studio_client.upload("Note", created_note["id"], str(attachment_path))
        LOGGER.info("Synced note on %s", vendor_version["code"])

    return messages


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("playlist", help="Review playlist name (studio-side naming)")
    parser.add_argument("vendor_type", choices=["cg", "tracking"], help="Which vendor round-trip this playlist belongs to")
    parser.add_argument("--config", default="vendor_pipeline_config.yaml", help="Path to config YAML")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    config = NoteSyncConfig.from_yaml(args.config)

    studio_client = config.studio_client()
    vendor_client = config.vendor_client()

    studio_playlist_name = args.playlist
    vendor_playlist_name = f"{config.cg_vendor_name}_{args.playlist}" if args.vendor_type == "cg" else args.playlist

    studio_lookup = build_studio_version_lookup(studio_client, config.studio_project_id, studio_playlist_name)
    vendor_fields = ["id", "code", "open_notes", "sg_status_list", "client_approved"]
    vendor_versions = find_playlist_versions(vendor_client, config.vendor_project_id, vendor_playlist_name, vendor_fields)
    LOGGER.info("Found %d version(s) on vendor playlist '%s'", len(vendor_versions), vendor_playlist_name)

    errors = []
    for vendor_version in vendor_versions:
        if args.vendor_type == "cg":
            error = sync_cg_vendor_status(studio_client, studio_lookup, vendor_version, config)
        else:
            error = sync_tracking_vendor_status(studio_client, studio_lookup, vendor_version, config)
        if error:
            errors.append(error)
        errors.extend(sync_notes(studio_client, vendor_client, studio_lookup, vendor_version, config))

    for error in errors:
        LOGGER.warning(error)
    LOGGER.info("Done. %d error(s)/warning(s).", len(errors))


if __name__ == "__main__":
    main()
