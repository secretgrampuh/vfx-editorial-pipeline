"""Ingest a vendor delivery: extract, route into the shot tree, publish for review.

Takes a folder of vendor-delivered files (roto/CG frames back from the
roto vendor, or tracking data from the tracking vendor), auto-extracts
any .rar/.zip archives, routes every file into the correct shot's folder
on the shots drive by parsing its filename, and - for movie files -
creates a ShotGrid Version and uploads it to a review playlist, tagged
to the right Shot and Task.

Which vendor a delivery is from is inferred from the source path: it
must contain one of the folder-name markers configured under
vendor_paths (e.g. a folder literally named "RotoVendorToStudio" or
"TrackingVendorToStudio" somewhere in its path), matching the VendorShare
folder convention the rest of this pipeline uses.
"""

import argparse
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from glob import glob
from pathlib import Path

import patoolib
import shotgun_api3
import yaml

LOGGER = logging.getLogger("delivery_sorter")

DELIVERABLE_EXTENSIONS = [".mov", ".nk", ".exr", ".jpeg", ".jpg", ".abc", ".ma", ".obj"]


@dataclass
class VendorRoute:
    path_marker: str
    task_type: str
    uploader_id: int


@dataclass
class PipelineConfig:
    project_prefix: str
    shots_root: Path
    studio_server: str
    studio_script_name: str
    studio_script_key: str
    studio_project_id: int
    roto_route: VendorRoute
    tracking_route: VendorRoute

    @classmethod
    def from_yaml(cls, config_path):
        with open(config_path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        project = raw["project"]
        studio = raw["shotgrid_studio"]
        uploaders = raw["uploader_ids"]
        vendor_paths = raw["vendor_paths"]
        return cls(
            project_prefix=project["prefix"],
            shots_root=Path(project["shots_root"]),
            studio_server=studio["server"],
            studio_script_name=studio["script_name"],
            studio_script_key=studio["script_key"],
            studio_project_id=int(studio["project_id"]),
            roto_route=VendorRoute(
                path_marker=vendor_paths["roto_marker"],
                task_type="Comp",
                uploader_id=int(uploaders["comp"]),
            ),
            tracking_route=VendorRoute(
                path_marker=vendor_paths["tracking_marker"],
                task_type="Tracking",
                uploader_id=int(uploaders["tracking"]),
            ),
        )

    def studio_client(self):
        return shotgun_api3.Shotgun(self.studio_server, self.studio_script_name, self.studio_script_key)


# --- Archive handling -------------------------------------------------------

def extract_archives(root_path):
    """Extract every .rar/.zip under root_path, marking each as done so a
    re-run doesn't re-extract it. Loops instead of recursing so a large
    delivery with many nested archives doesn't risk hitting the recursion
    limit."""
    while True:
        archives = [
            y for x in os.walk(root_path)
            for extension in ("*.rar", "*.zip")
            for y in glob(os.path.join(x[0], extension))
        ]
        if not archives:
            return
        for archive_path in archives:
            output_dir = os.path.join(os.path.dirname(archive_path), os.path.splitext(os.path.basename(archive_path))[0])
            if not os.path.exists(output_dir):
                LOGGER.info("Extracting %s", os.path.basename(archive_path))
                patoolib.extract_archive(archive_path, outdir=output_dir)
            os.rename(archive_path, archive_path + ".done")


# --- Naming / discovery -------------------------------------------------------

def quick_parse_code(filename, project_prefix):
    name = os.path.basename(filename)
    if "_Sh" not in name:
        LOGGER.error("No _Sh token in filename: %s", name)
        return None, None
    seq_code = name.split("_Sh")[0]
    if project_prefix not in seq_code:
        LOGGER.error("No %s token in: %s", project_prefix, name)
        return None, None
    seq_code = seq_code[seq_code.index(project_prefix):]
    shot_code = seq_code + "_Sh" + name.split("_Sh")[1].split("_")[0].split(".")[0]
    return seq_code, shot_code


def build_local_shot_lookup(shots_root, project_prefix):
    """Scene -> {shot: scene} for every shot folder actually on disk, used
    to sanity-check a delivered file's shot code before routing it."""
    lookup = {}
    for scene in os.listdir(shots_root):
        if not scene.startswith(f"{project_prefix}_"):
            continue
        scene_dir = os.path.join(shots_root, scene)
        if not os.path.isdir(scene_dir):
            continue
        for shot in os.listdir(scene_dir):
            if shot.startswith(f"{project_prefix}_"):
                lookup[shot] = scene
    return lookup


def gather_deliverable_files(root_path):
    return [
        y for x in os.walk(root_path)
        for extension in DELIVERABLE_EXTENSIONS
        for y in glob(os.path.join(x[0], f"*{extension}"))
    ]


# --- Routing into the shot tree ----------------------------------------------

def parse_cg_delivery_path(filename, shot_code):
    """CG/roto deliveries encode <CG>_<group>_<pass>_V## after the shot
    code, e.g. ..._Sh01230_CG_BG_BEAUTY_TINCTURE_V03.exr. Layout deliveries
    skip the pass token entirely."""
    base_name = os.path.basename(filename)
    remainder = base_name.split(shot_code)[1][1:]
    if "LAYOUT" in base_name:
        return "CG", "LAYOUT", "LAYOUT"
    tokens = remainder.split("_")
    cg_folder, group_folder = tokens[0], tokens[1]
    pass_and_version = remainder.split(group_folder, 1)[1][1:]
    pass_type = re.split("_[Vv]", pass_and_version)[0]
    return cg_folder, group_folder, pass_type


def route_cg_delivery(file_path, seq_code, shot_code, shots_root):
    base_name = os.path.basename(file_path)
    cg_folder, group_folder, pass_type = parse_cg_delivery_path(file_path, shot_code)
    version_match = re.search(r"_[Vv](\d+)", base_name)
    version = f"V{version_match.group(1)}" if version_match else "V01"

    if group_folder == "LAYOUT":
        destination = shots_root / seq_code / shot_code / cg_folder / group_folder / version / base_name
    else:
        destination = shots_root / seq_code / shot_code / cg_folder / group_folder / pass_type / version / base_name

    if destination.exists():
        LOGGER.info("%s already delivered to %s", base_name, destination)
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(file_path, destination)
    LOGGER.info("Copied %s to %s", base_name, destination)
    return destination


def route_tracking_delivery(file_path, seq_code, shot_code, shots_root):
    """Tracking deliveries mostly go straight into TRACKING/, except
    undistorted frame sequences, which get a version subfolder if the
    vendor tagged one in the filename."""
    base_name = os.path.basename(file_path)
    if ".1" in base_name and base_name.endswith(".jpeg"):
        destination = shots_root / seq_code / shot_code / "TRACKING" / "UNDISTORTED" / base_name
        version_match = re.search(r"_[Vv]0*(\d+)", base_name.split(".")[0])
        if version_match:
            version = f"v0{version_match.group(1)}"
            destination = shots_root / seq_code / shot_code / "TRACKING" / "UNDISTORTED" / version / base_name
    else:
        destination = shots_root / seq_code / shot_code / "TRACKING" / base_name

    if destination.exists():
        LOGGER.info("%s already delivered to %s", base_name, destination)
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(file_path, destination)
    LOGGER.info("Copied %s to %s", base_name, destination)
    return destination


# --- ShotGrid -----------------------------------------------------------------

def build_shot_lookup(sg_client, project_id):
    shots = sg_client.find("Shot", [["project", "is", {"type": "Project", "id": project_id}]], ["id", "code"])
    return {shot["code"]: shot["id"] for shot in shots}


def build_task_lookup(sg_client, project_id, task_step_contains):
    tasks = sg_client.find(
        "Task", [["project", "is", {"type": "Project", "id": project_id}]],
        ["id", "entity", "step"],
    )
    return {
        task["entity"]["name"]: task["id"]
        for task in tasks
        if task_step_contains.lower() in str(task["step"]).lower()
    }


def find_or_create_playlist(sg_client, project_id, playlist_name):
    existing = sg_client.find_one("Playlist", [["code", "is", playlist_name]])
    if existing is not None:
        return existing["id"]
    created = sg_client.create("Playlist", {"project": {"type": "Project", "id": project_id}, "code": playlist_name})
    LOGGER.info("Created new playlist: %s", playlist_name)
    return created["id"]


def version_already_on_playlist(sg_client, version_code, playlist_name):
    results = sg_client.find("Version", [["code", "is", version_code]], ["code", "playlists"])
    return any(playlist["name"] == playlist_name for result in results for playlist in result["playlists"])


def create_and_upload_version(sg_client, movie_path, shot_id, task_id, playlist_id, playlist_name, uploader_id, project_id, retries=2):
    version_name = os.path.splitext(os.path.basename(movie_path))[0]
    data = {
        "project": {"type": "Project", "id": project_id},
        "code": version_name,
        "sg_first_frame": 1001,
        "playlists": [{"id": playlist_id, "name": playlist_name, "type": "Playlist"}],
        "sg_task": {"type": "Task", "id": task_id},
        "user": {"type": "HumanUser", "id": uploader_id},
    }
    if shot_id is not None:
        data["entity"] = {"type": "Shot", "id": shot_id}

    created = sg_client.create("Version", data)
    for attempt in range(retries + 1):
        try:
            sg_client.upload("Version", created["id"], movie_path, field_name="sg_uploaded_movie")
            return created
        except Exception as error:  # noqa: BLE001 - ShotGrid uploads are flaky by nature
            LOGGER.warning("Upload attempt %d failed for %s: %s", attempt + 1, version_name, error)
            time.sleep(2)
    LOGGER.error("Could not upload %s after %d attempts", version_name, retries + 1)
    return created


# --- Orchestration --------------------------------------------------------------

def default_playlist_name(source_path, config):
    date_stamp = time.strftime("%m-%d-%Y")
    if config.roto_route.path_marker in source_path:
        return f"{config.roto_route.path_marker}_{date_stamp}"
    if config.tracking_route.path_marker in source_path:
        return f"{config.tracking_route.path_marker}_{date_stamp}"
    raise ValueError(
        f"Source path doesn't contain a known vendor marker "
        f"({config.roto_route.path_marker!r} or {config.tracking_route.path_marker!r}): {source_path}"
    )


def route_for_vendor(source_path, config):
    if config.roto_route.path_marker in source_path:
        return config.roto_route, route_cg_delivery
    if config.tracking_route.path_marker in source_path:
        return config.tracking_route, route_tracking_delivery
    raise ValueError(f"Source path doesn't contain a known vendor marker: {source_path}")


def process_delivery(source_path, playlist_name, config, sg_client, local_shot_lookup, sg_shot_lookup, task_lookup, playlist_id):
    route, router = route_for_vendor(source_path, config)
    files = gather_deliverable_files(source_path)
    LOGGER.info("Found %d deliverable file(s) under %s", len(files), source_path)

    errors = {}
    for file_path in files:
        seq_code, shot_code = quick_parse_code(file_path, config.project_prefix)
        if shot_code is None:
            continue
        if shot_code not in local_shot_lookup:
            errors[shot_code] = f"{shot_code} does not exist on disk"
            continue
        if local_shot_lookup[shot_code] != seq_code:
            errors[shot_code] = f"{seq_code} does not match the shot's real scene folder"
            continue
        router(file_path, seq_code, shot_code, config.shots_root)

    movie_files = [f for f in files if f.lower().endswith(".mov")]
    for movie_path in movie_files:
        seq_code, shot_code = quick_parse_code(movie_path, config.project_prefix)
        if shot_code is None or shot_code in errors:
            continue
        version_name = os.path.splitext(os.path.basename(movie_path))[0]
        if version_already_on_playlist(sg_client, version_name, playlist_name):
            LOGGER.info("%s already on playlist %s", version_name, playlist_name)
            continue
        if shot_code not in task_lookup:
            errors[shot_code] = f"{shot_code} has no matching {route.task_type} task"
            continue
        shot_id = sg_shot_lookup.get(shot_code)
        create_and_upload_version(
            sg_client, movie_path, shot_id, task_lookup[shot_code], playlist_id, playlist_name,
            route.uploader_id, config.studio_project_id,
        )

    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="Folder containing the vendor delivery to ingest")
    parser.add_argument("playlist", nargs="?", default=None, help="Review playlist name (auto-generated by vendor+date if omitted)")
    parser.add_argument("--config", default="vendor_pipeline_config.yaml", help="Path to config YAML")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    config = PipelineConfig.from_yaml(args.config)
    playlist_name = args.playlist or default_playlist_name(args.path, config)
    LOGGER.info("Using playlist: %s", playlist_name)

    extract_archives(args.path)

    sg_client = config.studio_client()
    playlist_id = find_or_create_playlist(sg_client, config.studio_project_id, playlist_name)
    local_shot_lookup = build_local_shot_lookup(config.shots_root, config.project_prefix)
    sg_shot_lookup = build_shot_lookup(sg_client, config.studio_project_id)

    route, _router = route_for_vendor(args.path, config)
    task_lookup = build_task_lookup(sg_client, config.studio_project_id, route.task_type)

    errors = process_delivery(
        args.path, playlist_name, config, sg_client, local_shot_lookup, sg_shot_lookup, task_lookup, playlist_id
    )

    LOGGER.info("### Error report ###")
    for item, message in errors.items():
        LOGGER.warning("%s: %s", item, message)

    LOGGER.info("Done.")


if __name__ == "__main__":
    main()
