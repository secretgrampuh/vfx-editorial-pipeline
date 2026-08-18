"""Build a Premiere-importable conform XML from newly delivered comps.

On a show taking vendor deliveries daily, a CG/roto vendor might land
anywhere from a handful to a few hundred finished comp movies at once.
Manually dragging each one into the editor's cut, in the right place, at
the right trim, doesn't scale past a handful of shots. This tool
automates that: it queries ShotGrid for comp Versions created in a date
range, finds their rendered files on the shots drive, and clones each
shot's position out of a picture-locked "template" timeline export -
trimming in/out points to match the new file's actual frame count - into
a fresh XML the editor imports straight into Premiere.

Multiple new versions of the same shot landing in one date range stack
onto separate video tracks instead of overwriting each other, so an
editor reviewing the batch can compare versions side by side rather than
only ever seeing the latest.

This is a generation step, not a live link: the resulting XML is a
snapshot of whatever was on the shots drive when it ran. A comp landing
after the fact needs a fresh run to show up on a new timeline - it won't
retroactively update an XML that's already been generated and imported.
"""

import argparse
import datetime
import logging
import os
import shutil
from dataclasses import dataclass
from glob import glob
from pathlib import Path

import cv2
import shotgun_api3
import yaml
from lxml import etree as ET

LOGGER = logging.getLogger("timeline_conform_generator")


@dataclass
class PipelineConfig:
    project_prefix: str
    shots_root: Path
    template_xml: Path
    output_dir: Path
    archive_dir: Path
    comp_file_glob: str
    excluded_path_markers: list
    excluded_creators: list
    studio_server: str
    studio_script_name: str
    studio_script_key: str
    studio_project_id: int

    @classmethod
    def from_yaml(cls, config_path):
        with open(config_path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        project = raw["project"]
        paths = raw["paths"]
        studio = raw["shotgrid_studio"]
        return cls(
            project_prefix=project["prefix"],
            shots_root=Path(paths["shots_root"]),
            template_xml=Path(paths["template_xml"]),
            output_dir=Path(paths["output_dir"]),
            archive_dir=Path(paths["archive_dir"]),
            comp_file_glob=project.get("comp_file_glob", "*comp*.mov"),
            excluded_path_markers=project.get("excluded_path_markers", []),
            excluded_creators=[c.lower() for c in project.get("excluded_creators", [])],
            studio_server=studio["server"],
            studio_script_name=studio["script_name"],
            studio_script_key=studio["script_key"],
            studio_project_id=int(studio["project_id"]),
        )

    def studio_client(self):
        return shotgun_api3.Shotgun(self.studio_server, self.studio_script_name, self.studio_script_key)


def find_new_comp_versions(sg_client, project_id, excluded_creators, start_date, end_date):
    """Every comp Version created in [start_date, end_date] (YYYYMMDD
    ints), split into (non_alpha, alpha) version-code lists by whether
    it's on a chromakey playlist. Versions from an excluded creator (e.g.
    an internal QC/test account, configured via excluded_creators) are
    skipped."""
    filters = [["project", "is", {"type": "Project", "id": project_id}]]
    fields = ["id", "code", "playlists", "created_at", "user"]
    versions = sg_client.find("Version", filters, fields)

    non_alpha, alpha = [], []
    for version in versions:
        version_code = version["code"].split(".")[0].lower()
        if "comp" not in version_code:
            continue
        creator = (version.get("user") or {}).get("name", "").lower()
        if creator in excluded_creators:
            continue
        date_created = int(str(version["created_at"]).split(" ")[0].replace("-", ""))
        if not (start_date <= date_created <= end_date):
            continue
        if "chroma" in str(version["playlists"]).lower():
            alpha.append(version_code)
        else:
            non_alpha.append(version_code)
    return non_alpha, alpha


def count_frames(path):
    """Real frame count of a rendered .mov, read straight off the file
    rather than trusted from anywhere else - this is what lets the conform
    trim a shot's in/out points to match what the vendor actually
    delivered, instead of assuming it matches the template's duration."""
    video = cv2.VideoCapture(str(path))
    total = int(video.get(cv2.CAP_PROP_FRAME_COUNT))
    video.release()
    return total


def shot_code_from_filename(filename):
    """Pull SHO_NNN_SCEN_ShNNNN out of a delivered comp's filename,
    normalizing the SH/Sh case the vendor happened to use."""
    for token in ("_Sh", "_SH"):
        if token in filename:
            prefix, rest = filename.split(token, 1)
            shot_number = rest.split("_")[0].split("-")[0].split(".")[0]
            return f"{prefix}_Sh{shot_number}"
    return None


def create_original_file_id(used_file_ids, index):
    """Generate a file-NN id that isn't already used in the template."""
    candidate = f"file-{len(used_file_ids) + index + 1}"
    while candidate in used_file_ids:
        index += 1
        candidate = f"file-{len(used_file_ids) + index + 1}"
    used_file_ids.append(candidate)
    return candidate


def index_template_clips(template_root, tree):
    """Walk video track 1 of the template timeline and index every
    clipitem by shot code. A shot appearing more than once on track 1
    (the same shot cut into the movie twice) gets its second node stored
    separately rather than silently dropped."""
    used_file_ids = ["file-0"]
    first_occurrence, second_occurrence = {}, {}

    for node in template_root.xpath("//video[1]//clipitem"):
        file_node = node.find("file")
        if file_node is None or file_node.find("pathurl") is None:
            continue
        used_file_ids.append(file_node.attrib["id"])

        name_node = node.find("name")
        if name_node is None or name_node.getparent() is not node:
            continue
        shot_code = (
            name_node.text.split(".")[0].split("_MERGED")[0].split("_V0")[0].split("_v0")[0].split("_Layer")[0]
        )
        if shot_code not in first_occurrence:
            first_occurrence[shot_code] = node
        else:
            second_occurrence[shot_code] = node

    return first_occurrence, second_occurrence, used_file_ids


def build_conform_node(template_node, new_file_path, new_file_basename, file_id, frame_counter=count_frames):
    """Clone a template clipitem, point it at the newly delivered file,
    and trim its duration/out point to match that file's real frame
    count if it differs from the template's. frame_counter is swappable
    so tests don't need a working OpenCV install to exercise the trim
    math."""
    cloned = ET.fromstring(ET.tostring(template_node))

    for name_node in cloned.findall("name"):
        if ".mov" in (name_node.text or ""):
            name_node.text = new_file_basename

    for value_node in cloned.xpath(".//value"):
        if value_node.text in ("200", "300"):
            value_node.text = "100"

    duration_node = cloned.find("duration")
    template_duration = duration_node.text
    frame_count = str(frame_counter(new_file_path))

    if frame_count != template_duration:
        duration_node.text = frame_count
        cloned.find("out").text = frame_count
        start_node, end_node = cloned.find("start"), cloned.find("end")
        new_end = str(int(start_node.text) + int(frame_count))
        if int(new_end) <= int(end_node.text):
            end_node.text = new_end

    file_node = cloned.find("file")
    file_node.attrib["id"] = file_id
    file_node.find("pathurl").text = new_file_path

    return cloned


def place_on_next_track(sequence_root, shot_code, cloned_node, track_usage):
    """Append a cloned clip onto the first video track (starting at
    track 2, since track 1 is the template's own picture lock) that
    doesn't already have this shot on it. Multiple new versions of the
    same shot in one run stack onto successive tracks instead of
    colliding. Logs (doesn't raise) if the template doesn't have enough
    video tracks to fit another layer."""
    for index, shots_on_track in enumerate(track_usage):
        if shot_code in shots_on_track:
            continue
        shots_on_track.append(shot_code)
        track_layer = index + 2
        track_node = sequence_root.find(f"*//video/track[{track_layer}]")
        if track_node is None:
            return f"Could not append {shot_code} to track {track_layer} - template doesn't have that many video tracks"
        track_node.append(cloned_node)
        return None
    return f"Could not append {shot_code} - ran out of track slots"


def build_conform_xml(config, comp_files, run_stamp, frame_counter=count_frames):
    """The main pass: match delivered comp files to their template
    position, clone/trim/re-path each, and write the resulting
    timeline."""
    tree = ET.parse(str(config.template_xml))
    template_root = tree.getroot()
    sequence_root = tree.getroot()

    first_occurrence, second_occurrence, used_file_ids = index_template_clips(template_root, tree)
    already_placed = {pathnode.text for pathnode in template_root.xpath("//video[1]//pathurl")}

    track_usage = [[] for _ in range(20)]
    errors = []

    for index, comp_path in enumerate(comp_files):
        comp_path = comp_path.replace("\\", "/").replace("SHOTS//", "SHOTS/")
        if any(marker in comp_path for marker in config.excluded_path_markers):
            continue

        basename = os.path.basename(comp_path)
        try:
            relative = comp_path.split("SHOTS")[1]
        except IndexError:
            errors.append(f"Delivered file not under a SHOTS folder, skipped: {comp_path}")
            continue
        new_file_path = f"file://{config.shots_root.as_posix()}{relative}"

        shot_code = shot_code_from_filename(basename)
        if shot_code is None:
            errors.append(f"Could not parse a shot code from filename: {basename}")
            continue
        if not shot_code.startswith(f"{config.project_prefix}_"):
            shot_code = f"{config.project_prefix}_{shot_code.split(config.project_prefix + '_')[-1]}"

        if new_file_path in already_placed:
            continue

        if shot_code in first_occurrence:
            template_node = first_occurrence[shot_code]
        elif shot_code in second_occurrence:
            template_node = second_occurrence[shot_code]
        else:
            errors.append(f"{shot_code} not found on the template timeline - probably misspelled: {comp_path}")
            continue

        file_id = create_original_file_id(used_file_ids, index)
        cloned_node = build_conform_node(template_node, new_file_path, basename, file_id, frame_counter)
        error = place_on_next_track(sequence_root, shot_code, cloned_node, track_usage)
        if error:
            errors.append(error)

    # Moves whatever ended up on tracks 3 and 4 to the end of the <video>
    # element's child list - lxml's .append() re-parents an element
    # already in the tree rather than duplicating it, so this doesn't
    # copy anything, it reorders it. Document order maps to stacking
    # order on import, so this puts those two tracks visually on top of
    # anything else appended above. Ported as-is from the original: the
    # source comment describing this step didn't actually match what the
    # code does, so this docstring describes the real behavior instead of
    # repeating that comment.
    video_node = sequence_root.find("*//video")
    track_3 = sequence_root.find("*//video/track[3]")
    track_4 = sequence_root.find("*//video/track[4]")
    if track_3 is not None and track_4 is not None:
        video_node.append(track_4)
        video_node.append(track_3)

    sequence_name = f"_allComps_{run_stamp}"
    for name_node in tree.getroot().xpath("//sequence/name"):
        name_node.text = sequence_name

    config.output_dir.mkdir(parents=True, exist_ok=True)
    config.archive_dir.mkdir(parents=True, exist_ok=True)
    for existing_xml in config.output_dir.glob("*.xml"):
        shutil.move(str(existing_xml), str(config.archive_dir / existing_xml.name))

    output_path = config.output_dir / f"_allComps_{run_stamp}.xml"
    tree.write(str(output_path), encoding="UTF-8")

    return output_path, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("start_date", help="YYYYMMDD - earliest comp creation date to include")
    parser.add_argument(
        "end_date",
        nargs="?",
        default=None,
        help="YYYYMMDD - latest comp creation date to include. Defaults to today.",
    )
    parser.add_argument("--config", default="timeline_conform_generator_config.yaml", help="Path to config YAML")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    config = PipelineConfig.from_yaml(args.config)
    start_date = int(args.start_date)
    end_date = int(args.end_date) if args.end_date else int(datetime.datetime.now().strftime("%Y%m%d"))

    sg_client = config.studio_client()
    non_alpha, alpha = find_new_comp_versions(
        sg_client, config.studio_project_id, config.excluded_creators, start_date, end_date
    )
    LOGGER.info("Comp versions found in range: %d non-alpha, %d alpha/chroma", len(non_alpha), len(alpha))

    all_comps = [
        y for x in os.walk(str(config.shots_root)) for y in glob(os.path.join(x[0], config.comp_file_glob))
    ]
    all_comps.sort(key=os.path.getmtime, reverse=True)

    non_alpha_files, alpha_files = [], []
    for path in all_comps:
        version_code = os.path.splitext(os.path.basename(path))[0].lower()
        if version_code in alpha:
            alpha_files.append(path)
        elif version_code in non_alpha:
            non_alpha_files.append(path)
    comp_files = non_alpha_files + alpha_files
    LOGGER.info("Matched %d delivered file(s) to a new comp version", len(comp_files))

    run_stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H_%M_%S")
    output_path, errors = build_conform_xml(config, comp_files, run_stamp)

    LOGGER.info("Wrote %s", output_path)
    if errors:
        LOGGER.warning("%d issue(s) while building the conform:", len(errors))
        for error in errors:
            LOGGER.warning("  %s", error)


if __name__ == "__main__":
    main()
