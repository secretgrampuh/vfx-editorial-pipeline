"""Mirror Task/Version status from a CG vendor's own ShotGrid site onto
this studio's own ShotGrid site.

Why this exists: the vendor needed to start work before the studio's own
ShotGrid instance was ready, so the vendor stood up their own separate
site in the meantime. That left two ShotGrid instances tracking the same
show, which someone had to reconcile by hand every day so the vendor's
approvals/notes/rejections actually showed up on the studio's side. This
tool automates that reconciliation instead. (In hindsight, one shared
instance from the start would have avoided the problem entirely - this is
a workaround for a scheduling gap, not a recommended architecture.)

Two modes, with uneven evidence behind them:
  --mode tasks     Mirrors a named Task's status (e.g. "Tracking"), driven
                    off the two ShotGrid sites' own Task records. This was
                    the one that actually ran hourly in production, for
                    about five months, with a clean run log throughout.
  --mode versions   Mirrors Version status the same way. The logic exists
                    in the source this was ported from, but it was never
                    wired into the production scheduler - always present,
                    always commented out. Included here because the logic
                    itself is real and testable, but treat it as
                    unproven, not as something known to have worked live.

Neither mode auto-creates a Task/Version that's missing on the studio
side - a vendor-side Task with no studio counterpart gets logged as a
flag for a human to look at, matching how the tool actually behaved in
production (it built the data for a create call but never issued it).
"""

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import shotgun_api3
import yaml

LOGGER = logging.getLogger("shotgrid_status_mirror")


@dataclass
class MirrorConfig:
    project_prefix: str
    log_dir: Path
    studio_server: str
    studio_script_name: str
    studio_script_key: str
    studio_project_id: int
    vendor_server: str
    vendor_script_name: str
    vendor_script_key: str
    vendor_project_id: int
    task_content: str
    task_skip_statuses: set
    task_status_map: dict
    version_skip_statuses: set
    version_status_map: dict

    @classmethod
    def from_yaml(cls, config_path):
        with open(config_path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        project = raw["project"]
        studio = raw["shotgrid_studio"]
        vendor = raw["shotgrid_cg_vendor"]
        task_mirror = raw["task_mirror"]
        version_mirror = raw["version_mirror"]
        return cls(
            project_prefix=project["prefix"],
            log_dir=Path(project["log_dir"]),
            studio_server=studio["server"],
            studio_script_name=studio["script_name"],
            studio_script_key=studio["script_key"],
            studio_project_id=int(studio["project_id"]),
            vendor_server=vendor["server"],
            vendor_script_name=vendor["script_name"],
            vendor_script_key=vendor["script_key"],
            vendor_project_id=int(vendor["project_id"]),
            task_content=task_mirror["task_content"],
            task_skip_statuses=set(task_mirror["studio_skip_statuses"]),
            task_status_map=dict(task_mirror["vendor_status_map"]),
            version_skip_statuses=set(version_mirror["studio_skip_statuses"]),
            version_status_map=dict(version_mirror["vendor_status_map"]),
        )

    def studio_client(self):
        return shotgun_api3.Shotgun(self.studio_server, self.studio_script_name, self.studio_script_key)

    def vendor_client(self):
        return shotgun_api3.Shotgun(self.vendor_server, self.vendor_script_name, self.vendor_script_key)


def build_task_status_lookup(sg_client, project_id, prefix, task_content):
    """shot_code -> {"id": task_id, "status": status} for every Shot's
    named Task, on one ShotGrid site."""
    shots = sg_client.find("Shot", [["project", "is", {"type": "Project", "id": project_id}]], ["id", "code"])
    lookup = {}
    for shot in shots:
        if not shot["code"].startswith(prefix):
            continue
        tasks = sg_client.find(
            "Task",
            [["entity", "is", {"type": "Shot", "id": shot["id"]}], ["content", "is", task_content]],
            ["id", "sg_status_list"],
        )
        if tasks:
            lookup[shot["code"]] = {"id": tasks[0]["id"], "status": tasks[0]["sg_status_list"]}
    return lookup


def mirror_task_status(studio_client, studio_lookup, vendor_lookup, config):
    """Bring the studio's Task statuses in line with the vendor's.

    Three cases per shot: both sites have the Task (update studio's status
    if it disagrees and isn't protected by studio_skip_statuses); only the
    studio has it (vendor dropped theirs - delete the stale studio Task);
    only the vendor has it (flag for manual creation, don't auto-create).
    """
    stats = {"updated": 0, "deleted": 0, "missing_on_studio": 0}
    for shot_code in sorted(set(studio_lookup) | set(vendor_lookup)):
        studio_entry = studio_lookup.get(shot_code)
        vendor_entry = vendor_lookup.get(shot_code)

        if studio_entry and not vendor_entry:
            try:
                studio_client.delete("Task", studio_entry["id"])
                LOGGER.info("%s: deleted stale %s Task (vendor no longer has one)", shot_code, config.task_content)
                stats["deleted"] += 1
            except Exception as exc:
                LOGGER.error("Could not delete %s Task for %s: %s", config.task_content, shot_code, exc)
            continue

        if vendor_entry and not studio_entry:
            LOGGER.warning(
                "%s has a %s Task on the vendor site with no counterpart on studio - "
                "flagging for manual creation, not auto-creating",
                shot_code, config.task_content,
            )
            stats["missing_on_studio"] += 1
            continue

        if studio_entry["status"] in config.task_skip_statuses:
            continue
        translated = config.task_status_map.get(vendor_entry["status"], vendor_entry["status"])
        if translated == studio_entry["status"]:
            continue
        try:
            studio_client.update("Task", studio_entry["id"], {"sg_status_list": translated})
            LOGGER.info("%s: %s -> %s", shot_code, studio_entry["status"], translated)
            stats["updated"] += 1
        except Exception as exc:
            LOGGER.error("Could not update %s Task for %s: %s", config.task_content, shot_code, exc)
    return stats


def build_version_status_lookup(sg_client, project_id, prefix):
    """version_code -> Version record, one ShotGrid site, keeping the most
    recently created record when a code appears more than once."""
    fields = ["id", "code", "sg_status_list", "created_at"]
    versions = sg_client.find("Version", [["project", "is", {"type": "Project", "id": project_id}]], fields)
    lookup = {}
    for version in versions:
        code = version["code"]
        if not code.startswith(prefix):
            continue
        existing = lookup.get(code)
        if existing is None or version["created_at"] > existing["created_at"]:
            lookup[code] = version
    return lookup


def mirror_version_status(studio_client, studio_lookup, vendor_lookup, config):
    stats = {"updated": 0}
    for code, vendor_version in vendor_lookup.items():
        studio_version = studio_lookup.get(code)
        if studio_version is None:
            continue
        vendor_status = vendor_version["sg_status_list"]
        if vendor_status in config.version_skip_statuses:
            continue
        translated = config.version_status_map.get(vendor_status, vendor_status)
        if translated == studio_version["sg_status_list"]:
            continue
        try:
            studio_client.update("Version", studio_version["id"], {"sg_status_list": translated})
            LOGGER.info("%s: %s -> %s", code, studio_version["sg_status_list"], translated)
            stats["updated"] += 1
        except Exception as exc:
            LOGGER.error("Could not update Version status for %s: %s", code, exc)
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["tasks", "versions"], default="tasks", help="Which entity type to mirror status for")
    parser.add_argument("--config", default="vendor_pipeline_config.yaml", help="Path to config YAML")
    args = parser.parse_args()

    config = MirrorConfig.from_yaml(args.config)
    config.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = config.log_dir / f"shotgrid_status_mirror_{args.mode}_{datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler()],
    )

    studio_client = config.studio_client()
    vendor_client = config.vendor_client()

    if args.mode == "tasks":
        studio_lookup = build_task_status_lookup(studio_client, config.studio_project_id, config.project_prefix, config.task_content)
        vendor_lookup = build_task_status_lookup(vendor_client, config.vendor_project_id, config.project_prefix, config.task_content)
        stats = mirror_task_status(studio_client, studio_lookup, vendor_lookup, config)
    else:
        studio_lookup = build_version_status_lookup(studio_client, config.studio_project_id, config.project_prefix)
        vendor_lookup = build_version_status_lookup(vendor_client, config.vendor_project_id, config.project_prefix)
        stats = mirror_version_status(studio_client, studio_lookup, vendor_lookup, config)

    LOGGER.info("Done. %s", stats)


if __name__ == "__main__":
    main()
