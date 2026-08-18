"""Forward approved CG renders out to the roto vendor.

Once a CG render has been approved on the studio's own ShotGrid (status
"apr" on a Version tied to this playlist), this finds where its rendered
frames actually live - either already staged from a prior delivery, or by
searching the shot's real CG folder on the shots drive - and copies them
into the outbound VendorShare folder the roto vendor pulls from.

Note: the original source for this tool had a second generation of logic
half-built alongside the live path - Conductor downloading, Google-Sheets
job-id logging, a CSV parser - none of it wired up or called. Only the
file-forwarding logic below was actually live in the version this was
ported from, so that's all that's here.
"""

import argparse
import logging
import os
import shutil
from dataclasses import dataclass
from glob import glob
from pathlib import Path

import shotgun_api3
import yaml

LOGGER = logging.getLogger("roto_forwarder")

# Render-pass tokens the version-name parser recognizes, longest/most
# specific first so a name isn't matched against a shorter substring of
# a more specific pass name.
PASS_TOKENS = [
    "BEAUTY_TINCTURE", "BEAUTY_VPEN_RASTER", "BEAUTYMESHING", "BEAUTY",
    "BRUSH_NW_ZEPTH", "BRUSH_NWZ", "BRUSH_LINES_SMALL", "BRUSH_LINES",
    "BRUSHLINESSMALL", "BRUSHLINES", "BRUSHNWZDEPTH", "BRUSH_SMALL",
    "BRUSHSMALL", "BRUSH", "LINES",
]


@dataclass
class PipelineConfig:
    project_prefix: str
    shots_root: Path
    vendor_share_root: Path
    studio_server: str
    studio_script_name: str
    studio_script_key: str
    studio_project_id: int
    cg_vendor_name: str
    roto_vendor_name: str

    @classmethod
    def from_yaml(cls, config_path):
        with open(config_path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        project = raw["project"]
        studio = raw["shotgrid_studio"]
        vendors = raw["vendors"]
        return cls(
            project_prefix=project["prefix"],
            shots_root=Path(project["shots_root"]),
            vendor_share_root=Path(project["vendor_share_root"]),
            studio_server=studio["server"],
            studio_script_name=studio["script_name"],
            studio_script_key=studio["script_key"],
            studio_project_id=int(studio["project_id"]),
            cg_vendor_name=vendors["cg_vendor_name"],
            roto_vendor_name=vendors["roto_vendor_name"],
        )

    def studio_client(self):
        return shotgun_api3.Shotgun(self.studio_server, self.studio_script_name, self.studio_script_key)


def strip_vendor_prefix(playlist_name, cg_vendor_name):
    for prefix in (f"{cg_vendor_name}_", f"{cg_vendor_name.lower()}_"):
        if playlist_name.startswith(prefix):
            return playlist_name[len(prefix):]
    return playlist_name


def build_shot_lookup(sg_client, project_id):
    shots = sg_client.find("Shot", [["project", "is", {"type": "Project", "id": project_id}]], ["id", "code"])
    lookup = {}
    for shot in shots:
        if shot["code"] in lookup:
            LOGGER.warning("%s appears more than once as a Shot", shot["code"])
        lookup[shot["code"]] = shot["id"]
    return lookup


def find_or_create_playlist(sg_client, project_id, playlist_code):
    existing = sg_client.find_one("Playlist", [["code", "is", playlist_code]])
    if existing is not None:
        return existing["id"]
    created = sg_client.create("Playlist", {"project": {"type": "Project", "id": project_id}, "code": playlist_code})
    return created["id"]


def find_approved_versions(sg_client, project_id, playlist_name):
    """Every Version on this playlist that's marked approved, as (shot_code, version_code)."""
    fields = ["id", "code", "entity", "playlists", "sg_status_list"]
    versions = sg_client.find("Version", [["project", "is", {"type": "Project", "id": project_id}]], fields)
    approved = []
    for version in versions:
        if not version["playlists"]:
            continue
        if version["playlists"][0]["name"] != playlist_name:
            continue
        if version["sg_status_list"] == "apr":
            approved.append((version["entity"]["name"], version["code"]))
    return approved


def parse_pass_and_version(version_code):
    """Split a CG version code into (base_name, pass_token, version_token)."""
    pass_token = "_CG"
    base_name = version_code.split("_CG")[0] + "_CG"
    for token in PASS_TOKENS:
        if token in version_code:
            pass_token = token
            base_name = version_code.split(token)[0]
            break
    version_token = "_V" + version_code.split("_V")[1].split(".")[0]
    return base_name, pass_token, version_token


def forward_approved_render(shot_code, version_code, config):
    """Locate an approved render's frames and copy them to both the
    inbound cache and the outbound roto-vendor share."""
    seq_code = shot_code.split("_Sh")[0]
    base_name, _pass_token, version_token = parse_pass_and_version(version_code)

    inbound_dir = config.vendor_share_root / f"{config.cg_vendor_name}To{config.roto_vendor_name}" / "SHOTS" / seq_code / shot_code
    outbound_dir = config.vendor_share_root / f"To{config.roto_vendor_name}" / "SHOTS" / seq_code / shot_code

    if inbound_dir.exists():
        LOGGER.info("Copying staged delivery for %s to %s", shot_code, outbound_dir)
        if not outbound_dir.exists():
            shutil.copytree(inbound_dir, outbound_dir)
        else:
            LOGGER.info("%s already forwarded", shot_code)
        return

    search_dir = config.shots_root / seq_code / shot_code / "CG"
    matches = [
        y for x in os.walk(str(search_dir)) for y in glob(os.path.join(x[0], f"*{base_name}*{version_token}*"))
    ]
    if not matches:
        LOGGER.warning("No rendered files found for %s %s under %s", shot_code, version_code, search_dir)
        return

    for source_path in matches:
        relative = Path(source_path).relative_to(config.shots_root)
        for destination_root in (inbound_dir.parent.parent, outbound_dir.parent.parent):
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copy(source_path, destination)
                LOGGER.info("Copied %s to %s", os.path.basename(source_path), destination)
            else:
                LOGGER.info("%s already exists at %s", os.path.basename(source_path), destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("playlist", help="Review playlist name (with or without the vendor prefix)")
    parser.add_argument("--config", default="vendor_pipeline_config.yaml", help="Path to config YAML")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    config = PipelineConfig.from_yaml(args.config)
    playlist_base = strip_vendor_prefix(args.playlist, config.cg_vendor_name)
    playlist_name = f"{config.cg_vendor_name}_{playlist_base}"

    sg_client = config.studio_client()
    find_or_create_playlist(sg_client, config.studio_project_id, playlist_name)

    approved = find_approved_versions(sg_client, config.studio_project_id, playlist_name)
    LOGGER.info("Found %d approved version(s) to forward", len(approved))

    for shot_code, version_code in approved:
        forward_approved_render(shot_code, version_code, config)

    LOGGER.info("Done.")


if __name__ == "__main__":
    main()
