"""Stamp per-shot Nuke comp scripts from a master template, across an entire show.

Walks a shots root, validates each shot's folder name against the show's
naming convention, auto-detects frame ranges (and multi-layer plates)
straight off the EXR sequences already on disk, and writes a per-shot Nuke
script from a token-substituted template. Three modes cover three different
templates:

- roto: the live-action roto/paint comp
- cg: the CG background/foreground beauty-pass comp (writes both BG and FG
  scripts from one template)
- directors-comp: a review comp built from the shot's latest approved COMP
  version, never overwriting a prior run - if a script already exists it
  writes the next version instead

directors-comp is meant to be run selectively, against specific shots, as
directors request updated composites - not blanket across the whole show
the way roto/cg are. Pass --shots to target specific ones; omit it to
run against every shot the naming convention discovers.
"""

import argparse
import copy
import datetime
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

LOGGER = logging.getLogger("templates_propagator")


@dataclass
class PipelineConfig:
    project_prefix: str
    shots_root: Path
    roto_template: Path
    cg_template: Path
    directors_comp_template: Path
    log_dir: Path

    @classmethod
    def from_yaml(cls, config_path):
        with open(config_path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        project = raw["project"]
        paths = raw["paths"]
        template_dir = Path(paths["template_dir"])
        return cls(
            project_prefix=project["prefix"],
            shots_root=Path(paths["shots_root"]),
            roto_template=template_dir / paths["roto_template"],
            cg_template=template_dir / paths["cg_template"],
            directors_comp_template=template_dir / paths["directors_comp_template"],
            log_dir=Path(paths["log_dir"]),
        )


# --- Shot discovery -----------------------------------------------------

def get_scene_code_from_shot_code(shot_code):
    """Strip the trailing ShNNNNN token off a shot code to get its scene code."""
    shot_token = shot_code.split("_")[-1]
    shot_token_len = len(shot_token) + 1
    if shot_token_len < 7 or shot_token_len > 9:
        return ""
    return shot_code[0:-shot_token_len]


def is_code_valid(code, project_prefix):
    """Validate a scene or shot code against <prefix>_NNN_... naming."""
    pattern = rf"^{re.escape(project_prefix)}_[0-9][0-9][0-9]_.+?"
    if re.search(pattern, code) is None:
        LOGGER.error("Code invalid: %s", code)
        return False
    return True


def list_shot_dirs(scene_dir, project_prefix):
    """List shot subfolders of a scene dir that match the naming convention."""
    LOGGER.info("Listing shot directories in scene dir: %s", scene_dir)
    shots, shot_dirs = [], []
    for name in os.listdir(scene_dir):
        full_path = os.path.join(scene_dir, name)
        if os.path.isdir(full_path) and is_code_valid(name, project_prefix):
            shots.append(name)
            shot_dirs.append(full_path)
            plate_dir = os.path.join(full_path, "PLATES", "source")
            if not os.path.isdir(plate_dir):
                LOGGER.error("Plate source folder not found: %s", plate_dir)
    return shots, shot_dirs


def list_all_shots(shots_root, project_prefix):
    """Walk shots_root for every scene/shot folder matching the naming convention."""
    LOGGER.info("Listing shots in root dir: %s", shots_root)
    scenes, scene_dirs, shots, shot_dirs = [], [], [], []
    for name in os.listdir(shots_root):
        full_path = os.path.join(shots_root, name)
        if os.path.isdir(full_path) and is_code_valid(name, project_prefix):
            scenes.append(name)
            scene_dirs.append(full_path)
            scene_shots, scene_shot_dirs = list_shot_dirs(full_path, project_prefix)
            shots.extend(scene_shots)
            shot_dirs.extend(scene_shot_dirs)
    return scenes, scene_dirs, shots, shot_dirs


# --- Frame range / version discovery -------------------------------------

def _extract_frame_from_exr(file_path, first_frame, last_frame):
    if not os.path.isfile(file_path):
        return first_frame, last_frame
    base, extension = os.path.splitext(file_path)
    if extension != ".exr":
        return first_frame, last_frame
    frame_number = int(base.split(".")[-1])
    return min(first_frame, frame_number), max(last_frame, frame_number)


def get_shot_plate_frame_range(shot_dir):
    """Auto-detect a shot's frame range from its PLATES/source EXRs,
    handling both a single sequence and multi-layer (LayerNN) subfolders."""
    first_frame, last_frame, num_plates = 9999, 0, 0
    plate_dir = os.path.join(shot_dir, "PLATES", "source")
    if not os.path.isdir(plate_dir):
        LOGGER.error("Source folder not found: %s", plate_dir)
        return first_frame, last_frame, 1

    for name in os.listdir(plate_dir):
        full_path = os.path.join(plate_dir, name)
        if os.path.isfile(full_path):
            first_frame, last_frame = _extract_frame_from_exr(full_path, first_frame, last_frame)
        elif os.path.isdir(full_path) and "layer" in full_path.lower():
            num_plates += 1
            for exr_name in os.listdir(full_path):
                exr_path = os.path.join(full_path, exr_name)
                first_frame, last_frame = _extract_frame_from_exr(exr_path, first_frame, last_frame)

    return first_frame, last_frame, max(num_plates, 1)


def get_frame_range(sequence_dir):
    """Auto-detect a frame range from any folder of EXRs (e.g. a COMP version)."""
    first_frame, last_frame = 9999, 0
    if not os.path.isdir(sequence_dir):
        LOGGER.error("Base folder not found: %s", sequence_dir)
        return 1001, 1001

    for name in os.listdir(sequence_dir):
        full_path = os.path.join(sequence_dir, name)
        if not os.path.isfile(full_path):
            continue
        base, extension = os.path.splitext(full_path)
        if extension != ".exr":
            continue
        try:
            frame_number = int(base.split(".")[-1])
        except ValueError:
            LOGGER.warning("File name does not contain a frame number: %s", base)
            continue
        first_frame, last_frame = min(first_frame, frame_number), max(last_frame, frame_number)
    return first_frame, last_frame


def extract_version_from_path(path):
    """Pull a VNN-style version token out of a file/folder name, default V01."""
    base = os.path.basename(path) if os.path.isfile(path) else path
    base = os.path.splitext(base)[0] if os.path.isfile(path) else base
    for char in (".", " ", "/", "\\"):
        base = base.replace(char, "_")
    for token in reversed(base.split("_")):
        if re.search(r"^[vV]\d{2,4}$", token):
            return token
    return "V01"


def get_latest_version(base_path, name_regex=".*", only_folders=False):
    """Find the highest VNN-versioned file/subfolder under base_path.

    Returns (version_string, path), or (None, "") if nothing matched.
    Accepts either a directory to scan or a single file to read the
    version straight off its name.
    """
    if not os.path.exists(base_path):
        LOGGER.error("Version discovery path does not exist: %s", base_path)
        return None, ""

    latest_version, latest_number, latest_path, found = "V01", 1, "", False

    if os.path.isdir(base_path):
        for name in os.listdir(base_path):
            if not re.search(name_regex, name):
                continue
            full_path = os.path.join(base_path, name)
            if os.path.isfile(full_path) and only_folders:
                continue
            version = extract_version_from_path(full_path)
            version_number = int(version[1:])
            if version_number >= latest_number:
                latest_version, latest_number, latest_path, found = version, version_number, full_path, True
    elif os.path.isfile(base_path):
        file_name = os.path.splitext(base_path)[0]
        if not re.search(name_regex, file_name):
            return None, ""
        version = extract_version_from_path(base_path)
        version_number = int(version[1:])
        if version_number >= latest_number:
            latest_version, latest_number, latest_path, found = version, version_number, base_path, True

    return (latest_version, latest_path) if found else (None, "")


def increment_version(version):
    """V03 -> V04, preserving the original zero-padding width."""
    prefix, number = version[0], int(version[1:])
    return prefix + str(number + 1).zfill(len(version) - 1)


def construct_sequence_name(comp_dir):
    """Discover an EXR sequence's name pattern in comp_dir, frame number
    replaced with ####. Returns None if no matching sequence is found."""
    if not os.path.isdir(comp_dir):
        LOGGER.error("Comp dir path does not exist: %s", comp_dir)
        return None
    for name in os.listdir(comp_dir):
        full_path = os.path.join(comp_dir, name)
        if not os.path.isfile(full_path) or os.path.splitext(full_path)[1] != ".exr":
            continue
        if not re.search(r".*[vV]\d{2,3}.\d{4,8}.exr", name):
            continue
        sequence_name = re.sub(r"(\d+)(?=\.\w+$)", "####", name)
        return os.path.join(comp_dir, sequence_name)
    LOGGER.warning("Sequence detection failed for folder: %s", comp_dir)
    return None


# --- Template stamping ----------------------------------------------------

def create_script_from_template(template_path, output_path, substitutions):
    """Write output_path as a copy of template_path with every (find,
    replace) pair in substitutions applied."""
    if not os.path.isfile(template_path):
        LOGGER.critical("Template file not accessible: %s", template_path)
        return
    if not substitutions:
        LOGGER.warning("No substitutions given, not writing %s", output_path)
        return

    with open(template_path, "r", encoding="utf-8") as handle:
        content = handle.read()
    for find, replace in substitutions:
        content = content.replace(find, replace)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(content)


def _posix(path):
    """Nuke expects forward slashes in script text regardless of platform."""
    return Path(path).as_posix()


def _run_log_path(log_dir, run_name, run_stamp):
    log_dir = log_dir / run_name
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{run_name}_{run_stamp}.txt"


def process_roto_template(config, run_stamp):
    """Stamp the live-action roto/paint template across every shot."""
    _, _, shots, _ = list_all_shots(str(config.shots_root), config.project_prefix)
    LOGGER.info("Total shots found: %d", len(shots))

    log_path = _run_log_path(config.log_dir, "roto_propagator", run_stamp)
    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"# SHOTS_ROOT {config.shots_root}\n# SHOT_COUNT {len(shots)}\n")
        for index, shot in enumerate(shots, start=1):
            scene_code = get_scene_code_from_shot_code(shot)
            shot_dir = config.shots_root / scene_code / shot
            first_frame, last_frame, num_plates = get_shot_plate_frame_range(str(shot_dir))

            for layer_index in range(1, num_plates + 1):
                layer_suffix = f"_Layer{layer_index:02}" if num_plates > 1 else ""
                layer_folder = f"{shot}{layer_suffix}/" if num_plates > 1 else ""
                plates = _posix(shot_dir / "PLATES")
                subs = [
                    ("file READ_PATH_PLATE", f"file {plates}/source/{layer_folder}{shot}{layer_suffix}.####.exr"),
                    ("file READ_PATH_ROTO", f"file {plates}/roto/{layer_folder}V01/{shot}_roto{layer_suffix}_V01.####.png"),
                    ("file WRITE_PATH_TINCTURE.exr", f"file {plates}/tincture/{layer_folder}V01/{shot}_tincture{layer_suffix}_V01.####.exr"),
                    ("file WRITE_PATH_OUTLINES.exr", f"file {plates}/outlines/{layer_folder}V01/{shot}_outlines{layer_suffix}_V01.####.exr"),
                    ("file WRITE_PATH_MOTION.exr", f"file {plates}/motion/{layer_folder}V01/{shot}_motion{layer_suffix}_V01.####.exr"),
                    ("file WRITE_PATH_VPEN_TRIANGLE.exr", f"file {plates}/vPen_Triangle/{layer_folder}V01/{shot}_vPen_Triangle{layer_suffix}_V01.####.exr"),
                    ("file WRITE_PATH_VPEN_LINES_HORIZONTAL.exr", f"file {plates}/vPen_Lines_H/{layer_folder}V01/{shot}_vPen_Lines_H{layer_suffix}_V01.####.exr"),
                    ("file WRITE_PATH_VPEN_LINES.exr", f"file {plates}/vPen_Lines/{layer_folder}V01/{shot}_vPen_Lines{layer_suffix}_V01.####.exr"),
                    ("file WRITE_PATH_VPEN_RASTER.exr", f"file {plates}/vPen_Raster/{layer_folder}V01/{shot}_vPen_Raster{layer_suffix}_V01.####.exr"),
                    ("shots_root W:/SHO/SHOTS", f"shots_root {_posix(config.shots_root)}"),
                    ("scene_code SHO_000_AAA", f"scene_code {scene_code}"),
                    ("shot_code SHO_000_AAA_Sh00000", f"shot_code {shot}"),
                    ("first_frame 1001", f"first_frame {first_frame}"),
                    ("last_frame 1101", f"last_frame {last_frame}"),
                ]
                script_name = f"{shot}_Roto{layer_suffix}_V01.nk"
                output_path = shot_dir / "Nuke" / "Roto" / script_name
                create_script_from_template(config.roto_template, output_path, subs)
                log.write(f"{output_path}\n")

            if index % 100 == 0:
                LOGGER.info("Shots processed: %d", index)


def process_cg_template(config, run_stamp):
    """Stamp the CG BG and FG beauty-pass templates across every shot."""
    _, _, shots, _ = list_all_shots(str(config.shots_root), config.project_prefix)
    LOGGER.info("Total shots found: %d", len(shots))

    log_path = _run_log_path(config.log_dir, "cg_propagator", run_stamp)
    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"# SHOTS_ROOT {config.shots_root}\n# SHOT_COUNT {len(shots)}\n")
        for index, shot in enumerate(shots, start=1):
            scene_code = get_scene_code_from_shot_code(shot)
            shot_dir = config.shots_root / scene_code / shot
            first_frame, last_frame, num_plates = get_shot_plate_frame_range(str(shot_dir))

            for layer_index in range(1, num_plates + 1):
                layer_suffix = f"_Layer{layer_index:02}" if num_plates > 1 else ""
                base_subs = [
                    ("shots_root W:/SHO/SHOTS", f"shots_root {_posix(config.shots_root)}"),
                    ("scene_code SHO_000_AAA", f"scene_code {scene_code}"),
                    ("shot_code SHO_000_AAA_Sh00000", f"shot_code {shot}"),
                    ("first_frame 1001", f"first_frame {first_frame}"),
                    ("last_frame 1101", f"last_frame {last_frame}"),
                ]
                for group, script_suffix in (("BG", "BG"), ("FG", "FG")):
                    subs = copy.deepcopy(base_subs)
                    element_dir = _posix(shot_dir / "CG" / group)
                    subs.append(("file READ_PATH_CG_BEAUTY", f"file {element_dir}/BEAUTY/V01/{shot}_CG_{group}_BEAUTY_V01.####.exr"))
                    subs.append((
                        "file WRITE_PATH_CG_BEAUTY_VPEN_RASTER.exr",
                        f"file {element_dir}/BEAUTY_VPEN_RASTER/V01/{shot}_CG_{group}_BEAUTY_VPEN_RASTER_V01.####.exr",
                    ))
                    subs.append((
                        "file WRITE_PATH_CG_BEAUTY_TINCTURE.exr",
                        f"file {element_dir}/BEAUTY_TINCTURE/V01/{shot}_CG_{group}_BEAUTY_TINCTURE_V01.####.exr",
                    ))
                    script_name = f"{shot}_CG_{script_suffix}_V01.nk"
                    output_path = shot_dir / "Nuke" / "CG" / script_name
                    create_script_from_template(config.cg_template, output_path, subs)
                    log.write(f"{output_path}\n")

            if index % 100 == 0:
                LOGGER.info("Shots processed: %d", index)


def process_directors_comp_template(config, run_stamp, shots=None):
    """Stamp a director's-comp review script per shot from its latest
    approved COMP version. Never overwrites an existing script - writes
    the next version instead."""
    if shots is None:
        _, _, shots, _ = list_all_shots(str(config.shots_root), config.project_prefix)
    LOGGER.info("Shots targeted: %d", len(shots))

    versionless_shots, existing_comps = [], []
    log_path = _run_log_path(config.log_dir, "directors_comp_propagator", run_stamp)

    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"# SHOTS_ROOT {config.shots_root}\n# SHOT_COUNT {len(shots)}\n")
        for shot in shots:
            scene_code = get_scene_code_from_shot_code(shot)
            shot_dir = config.shots_root / scene_code / shot
            comp_base_dir = shot_dir / "COMP"

            comp_version, comp_dir = get_latest_version(str(comp_base_dir), ".*", True)

            if comp_version is None:
                comp_version = "V01"
                LOGGER.warning("No COMP versions found for shot: %s", shot)
                versionless_shots.append(str(shot_dir))
                first_frame, last_frame = 1001, 1100
                comp_sequence = shot_dir / "COMP" / "V01" / f"{shot}_COMP_V01.####.exr"
            else:
                first_frame, last_frame = get_frame_range(comp_dir)
                comp_sequence = construct_sequence_name(comp_dir)
                if comp_sequence is None:
                    first_frame, last_frame = 1001, 1100
                    comp_sequence = shot_dir / "COMP" / comp_version / f"{shot}_COMP_{comp_version}.####.exr"

            write_path = shot_dir / "COMP" / "DIRECTOR_COMP" / f"{shot}_DirectorsComp.####.exr"
            subs = [
                ("first_frame 1001", f"first_frame {first_frame}"),
                ("last_frame 1002", f"last_frame {last_frame}"),
                ("first 1001", f"first {first_frame}"),
                ("last 1002", f"last {last_frame}"),
                ("origfirst 1001", f"origfirst {first_frame}"),
                ("origlast 1002", f"origlast {last_frame}"),
                ("file READ_PATH_COMP.exr", f"file {_posix(comp_sequence)}"),
                ("file WRITE_READ_PATH_COMP.exr", f"file {_posix(write_path)}"),
            ]

            script_name = f"{shot}_DirectorsComp_{comp_version}.nk"
            output_path = shot_dir / "Nuke" / script_name
            if output_path.exists():
                comp_version = increment_version(comp_version)
                script_name = f"{shot}_DirectorsComp_{comp_version}.nk"
                output_path = shot_dir / "Nuke" / script_name
                existing_comps.append(str(output_path))
                LOGGER.info("Script already exists, incrementing version: %s", output_path)

            create_script_from_template(config.directors_comp_template, output_path, subs)
            log.write(f"{output_path}\n")

        log.write("# SHOTS WITH NO DISCOVERED COMP VERSIONS\n")
        log.write("\n".join(versionless_shots) + "\n")
        log.write("# EXISTING SCRIPTS THAT WERE VERSION-INCREMENTED INSTEAD OF OVERWRITTEN\n")
        log.write("\n".join(existing_comps) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", required=True, choices=["roto", "cg", "directors-comp"])
    parser.add_argument("--config", default="templates_propagator_config.yaml", help="Path to config YAML")
    parser.add_argument(
        "--shots",
        nargs="*",
        default=None,
        help="Directors-comp only: specific shot codes to target. Omit to run against every discovered shot.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    config = PipelineConfig.from_yaml(args.config)
    run_stamp = datetime.datetime.now().strftime("%y%m%d_%H%M%S")

    if args.mode == "roto":
        process_roto_template(config, run_stamp)
    elif args.mode == "cg":
        process_cg_template(config, run_stamp)
    elif args.mode == "directors-comp":
        process_directors_comp_template(config, run_stamp, shots=args.shots)

    LOGGER.info("Done.")


if __name__ == "__main__":
    main()
