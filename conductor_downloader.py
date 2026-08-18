"""Pull review versions and finished renders from a CG vendor into the studio's pipeline.

The CG vendor renders on their own render farm via Conductor (a cloud
rendering service) and reviews their own work on their own separate
ShotGrid site. This tool, given a playlist name, pulls that vendor's
approved review movies down, republishes them as Versions on the studio's
own ShotGrid site linked to the right Shot/Task, and pulls the full-
resolution EXR sequences down from Conductor directly - then verifies
every sequence it pulled for missing frames.

Requires the `conductor` CLI to be installed and authenticated separately
(this only shells out to it), and network access to both ShotGrid sites.
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from glob import glob
from pathlib import Path

import shotgun_api3
import yaml

LOGGER = logging.getLogger("conductor_downloader")


@dataclass
class PipelineConfig:
    project_prefix: str
    shots_root: Path
    vendor_share_root: Path
    log_dir: Path
    conductor_cache_path: Path
    conductor_job_id_cutoff: int
    studio_server: str
    studio_script_name: str
    studio_script_key: str
    studio_project_id: int
    vendor_server: str
    vendor_script_name: str
    vendor_script_key: str
    vendor_project_id: int
    comp_uploader_id: int
    cg_vendor_name: str

    @classmethod
    def from_yaml(cls, config_path):
        with open(config_path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        project = raw["project"]
        studio = raw["shotgrid_studio"]
        vendor = raw["shotgrid_cg_vendor"]
        return cls(
            project_prefix=project["prefix"],
            shots_root=Path(project["shots_root"]),
            vendor_share_root=Path(project["vendor_share_root"]),
            log_dir=Path(project["log_dir"]),
            conductor_cache_path=Path(project["conductor_cache_path"]),
            conductor_job_id_cutoff=int(project["conductor_job_id_cutoff"]),
            studio_server=studio["server"],
            studio_script_name=studio["script_name"],
            studio_script_key=studio["script_key"],
            studio_project_id=int(studio["project_id"]),
            vendor_server=vendor["server"],
            vendor_script_name=vendor["script_name"],
            vendor_script_key=vendor["script_key"],
            vendor_project_id=int(vendor["project_id"]),
            comp_uploader_id=int(raw["uploader_ids"]["comp"]),
            cg_vendor_name=raw["vendors"]["cg_vendor_name"],
        )

    def studio_client(self):
        return shotgun_api3.Shotgun(self.studio_server, self.studio_script_name, self.studio_script_key)

    def vendor_client(self):
        return shotgun_api3.Shotgun(self.vendor_server, self.vendor_script_name, self.vendor_script_key)


# --- Naming / parsing -----------------------------------------------------

def quick_parse_code(filename, project_prefix):
    """Pull <PREFIX>_NNN_XXXX and its Sh##### shot code out of a filename.
    Returns (None, None) if the filename doesn't carry a recognizable code."""
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


# --- Conductor job-id cache -------------------------------------------------

def load_completed_job_cache(cache_path):
    if cache_path.exists():
        return set(json.loads(cache_path.read_text(encoding="utf-8")))
    return set()


def save_completed_job_cache(cache_path, completed_job_ids):
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(sorted(completed_job_ids)), encoding="utf-8")


# --- Frame QC ---------------------------------------------------------------

def find_missing_frames(shot_pass, path_to_search):
    """Scan an EXR sequence for gaps between frame 1001 and its highest frame."""
    base_name = shot_pass.split(".")[0]
    frames_on_disk = set()
    highest_frame = 0
    for result in (y for x in os.walk(path_to_search) for y in glob(os.path.join(x[0], "*.exr"))):
        frames_on_disk.add(os.path.basename(result))
        frame_number = int(result.split(".")[1])
        highest_frame = max(highest_frame, frame_number)

    missing = [
        f"{base_name}.{frame}.exr"
        for frame in range(1001, highest_frame + 1)
        if f"{base_name}.{frame}.exr" not in frames_on_disk
    ]
    return missing


# --- ShotGrid helpers --------------------------------------------------------

SHOT_FIELDS = ["id", "code", "entity"]
TASK_FIELDS = ["id", "content", "code", "entity"]


def build_shot_lookup(sg_client, project_id):
    shots = sg_client.find("Shot", [["project", "is", {"type": "Project", "id": project_id}]], SHOT_FIELDS)
    lookup = {}
    for shot in shots:
        if shot["code"] in lookup:
            LOGGER.warning("%s appears more than once as a Shot", shot["code"])
        lookup[shot["code"]] = shot["id"]
    return lookup


def build_task_lookup(sg_client, project_id, task_content):
    """Map shot code -> Task id for every Task whose content matches task_content."""
    tasks = sg_client.find("Task", [["project", "is", {"type": "Project", "id": project_id}]], TASK_FIELDS)
    return {
        task["entity"]["name"]: task["id"]
        for task in tasks
        if str(task["content"]).lower() == task_content.lower()
    }


def find_or_create_playlist(sg_client, project_id, playlist_code):
    existing = sg_client.find_one("Playlist", [["code", "is", playlist_code]])
    if existing is not None:
        return [{"id": existing["id"], "name": playlist_code, "type": "Playlist"}]
    created = sg_client.create("Playlist", {"project": {"type": "Project", "id": project_id}, "code": playlist_code})
    return [{"id": created["id"], "name": playlist_code, "type": "Playlist"}]


def version_already_on_playlist(sg_client, version_code, playlist_name):
    results = sg_client.find("Version", [["code", "is", version_code]], ["code", "playlists"])
    return any(playlist["name"].upper() == playlist_name.upper() for result in results for playlist in result["playlists"])


def create_version(sg_client, version_name, playlist_data, job_id, shot_lookup, task_lookup, config):
    """Create a Version for a downloaded delivery, inferring its shot/task/
    frame-path from the filename where the naming convention allows it."""
    LOGGER.info("Creating Version: %s", version_name)
    data = {
        "project": {"type": "Project", "id": config.studio_project_id},
        "code": version_name,
        "playlists": playlist_data,
        "sg_first_frame": 1001,
        "sg_job_id": job_id,
        "sg_uploaded_movie_frame_rate": 23.976,
        "user": {"type": "HumanUser", "id": config.comp_uploader_id},
    }

    if "_sh" not in version_name.lower():
        return sg_client.create("Version", data), None

    shot_code = version_name.split("_Sh")[0] + "_Sh" + version_name.split("_Sh")[1].split("_")[0].split(".")[0]
    if shot_code in task_lookup:
        data["sg_task"] = {"type": "Task", "id": task_lookup[shot_code]}
    else:
        LOGGER.error("%s has no corresponding task", version_name)
    if shot_code in shot_lookup:
        data["entity"] = {"type": "Shot", "id": shot_lookup[shot_code]}

    path_to_frames = None
    if "_v" in version_name.lower():
        seq_code = version_name.split("_Sh")[0]
        version_number = "V" + version_name.lower().split("_v")[1].split(".")[0]
        if "layout" in version_name.lower():
            path_to_frames = f"{config.shots_root}/{seq_code}/{shot_code}/CG/Layout/{version_number}"
        elif "cg" in version_name.lower():
            bg_or_fg = version_name.split("_CG_")[1].split("_")[0]
            pass_kind = version_name.split("_CG_")[1].split("_")[1]
            path_to_frames = f"{config.shots_root}/{seq_code}/{shot_code}/CG/{bg_or_fg}/{pass_kind}/{version_number}"
        else:
            LOGGER.error("%s: unsure where its frames should live", version_name)
        if path_to_frames:
            data["sg_path_to_frames"] = path_to_frames

    return sg_client.create("Version", data), path_to_frames


# --- Conductor CLI ------------------------------------------------------------

def parse_conductor_download_paths(stdout_text):
    """Pull real downloaded file paths out of the conductor CLI's stdout."""
    paths = []
    for token in stdout_text.replace("\\n", " ").split():
        if config_path_looks_real(token):
            normalized = token.replace("\\", "/")
            if normalized not in paths:
                paths.append(normalized)
    return paths


def config_path_looks_real(token):
    return "/" in token and "." in token


def download_from_conductor(job_id, log_dir):
    result = subprocess.run(
        ["conductor", "downloader", "--job_id", job_id, "--log_dir", str(log_dir)],
        shell=True, capture_output=True, text=True,
    )
    return parse_conductor_download_paths(str(result))


def copy_to_vendor_share(file_paths_by_delivery, vendor_share_root, shots_root, log_path):
    """Mirror every downloaded file's SHOTS-relative path under VendorShare."""
    shots_root_marker = f"{Path(shots_root).name}/"
    for delivery_name, file_paths in file_paths_by_delivery.items():
        start_time = time.time()
        for source_path in file_paths:
            try:
                relative = source_path.split(shots_root_marker)[1]
                destination = vendor_share_root / "SHOTS" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    shutil.copy(source_path, destination)
            except (IndexError, OSError) as error:
                LOGGER.error("Could not copy %s: %s", source_path, error)
        message = f"file {delivery_name} took {time.time() - start_time:.2f}s to copy to vendor share"
        LOGGER.info(message)
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(message + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("playlist", help="Name of the vendor review playlist to pull")
    parser.add_argument("--config", default="vendor_pipeline_config.yaml", help="Path to config YAML")
    parser.add_argument("--retries", type=int, default=2, help="Retry attempts for a failed download before giving up")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    config = PipelineConfig.from_yaml(args.config)

    config.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = config.log_dir / f"{args.playlist}_{time.strftime('%y%m%d_%H%M%S')}.txt"

    studio_playlist_name = f"{config.cg_vendor_name}_{args.playlist}"
    target_path = config.vendor_share_root / studio_playlist_name
    target_path.mkdir(parents=True, exist_ok=True)

    studio_sg = config.studio_client()
    vendor_sg = config.vendor_client()

    shot_lookup = build_shot_lookup(studio_sg, config.studio_project_id)
    comp_task_lookup = build_task_lookup(studio_sg, config.studio_project_id, "Comp")
    playlist_data = find_or_create_playlist(studio_sg, config.studio_project_id, studio_playlist_name)

    vendor_versions = vendor_sg.find(
        "Version",
        [["project", "is", {"type": "Project", "id": config.vendor_project_id}]],
        ["id", "code", "entity", "playlists", "sg_job_id", "sg_uploaded_movie"],
    )

    job_ids_to_download = {}
    for version in vendor_versions:
        if not any(p["name"] == args.playlist for p in version["playlists"]):
            continue

        file_name = version["code"]
        try:
            file_name_ext = version["sg_uploaded_movie"]["name"]
        except (TypeError, KeyError):
            file_name_ext = file_name + ".png"
        local_path = target_path / file_name_ext

        if not local_path.exists():
            LOGGER.info("Downloading review media for %s (job %s)", file_name, version["sg_job_id"])
            for attempt in range(args.retries + 1):
                try:
                    vendor_sg.download_attachment(version["sg_uploaded_movie"], file_path=str(local_path))
                    break
                except Exception as error:  # noqa: BLE001 - vendor SG attachment fetch is flaky by nature
                    LOGGER.warning("Attempt %d failed for %s: %s", attempt + 1, file_name, error)
                    time.sleep(2)

        if local_path.exists() and not version_already_on_playlist(studio_sg, file_name, studio_playlist_name):
            created, path_to_frames = create_version(
                studio_sg, file_name, playlist_data, version["sg_job_id"], shot_lookup, comp_task_lookup, config
            )
            try:
                studio_sg.upload("Version", created["id"], str(local_path), field_name="sg_uploaded_movie")
            except Exception as error:  # noqa: BLE001
                LOGGER.error("Could not upload %s: %s", file_name, error)

        job_ids_to_download[file_name] = version["sg_job_id"]

    completed_job_ids = load_completed_job_cache(config.conductor_cache_path)
    downloaded_files = {}
    for file_name, job_id in job_ids_to_download.items():
        if int(job_id) < config.conductor_job_id_cutoff:
            LOGGER.warning("%s has a job id purged from Conductor: %s", file_name, job_id)
            continue
        if str(job_id) in completed_job_ids:
            LOGGER.info("%s already downloaded, skipping", file_name)
            continue

        start_time = time.time()
        downloaded_files[file_name] = download_from_conductor(str(job_id), config.log_dir)
        completed_job_ids.add(str(job_id))
        message = f"Download for {file_name}, job {job_id} took {time.time() - start_time:.2f}s"
        LOGGER.info(message)
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(message + "\n")

    save_completed_job_cache(config.conductor_cache_path, completed_job_ids)
    copy_to_vendor_share(downloaded_files, config.vendor_share_root, config.shots_root, log_path)

    LOGGER.info("Checking missing frames for %d shots...", len(job_ids_to_download))
    missing_frame_report = {}
    for file_name in job_ids_to_download:
        _, shot_code = quick_parse_code(file_name, config.project_prefix)
        if shot_code is None:
            continue
        seq_code = shot_code.split("_Sh")[0]
        search_path = config.shots_root / seq_code / shot_code / "CG"
        for first_frame in glob(os.path.join(str(search_path), "**", "*1001*.exr"), recursive=True):
            pass_dir = os.path.dirname(first_frame)
            pass_name = os.path.basename(first_frame)
            missing = find_missing_frames(pass_name, pass_dir)
            if missing:
                missing_frame_report[pass_name] = missing

    with open(log_path, "a", encoding="utf-8") as log:
        log.write("\nMISSING FRAME REPORT:\n")
        for pass_name, missing in missing_frame_report.items():
            log.write(f"{pass_name} is missing {missing[0]} thru {missing[-1]}\n")

    for pass_name, missing in missing_frame_report.items():
        LOGGER.warning("%s is missing %s thru %s", pass_name, missing[0], missing[-1])

    LOGGER.info("Done. Log written to %s", log_path)


if __name__ == "__main__":
    main()
