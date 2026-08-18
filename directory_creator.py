"""Build per-shot folders and stamp the KEY Nuke comp for every shot in a show.

Consumes the per-shot reference XMLs produced by shot_reference_generator.py.
For each one, it:

1. reindexes any 0-indexed exported frame sequence so Nuke's 1-based frame
   range convention isn't broken
2. builds the full per-shot folder tree and copies the matching source
   frames into it
3. stamps a per-shot copy of the KEY Nuke template with this shot's output
   path, duration, and source-plate path

This is deliberately the subset of the original tool that's fully verified.
A shot-scale-aware preset system for the show's signature rotoscope-brush
treatment (separate MED/WIDE roto templates and CG background-pass
templates, tuned per shot scale) was also built alongside this, but the
master template it depended on was never finished, so it isn't included
here rather than shipping unverified.
"""

import argparse
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml
from lxml import etree as ET

LOGGER = logging.getLogger("directory_creator")


@dataclass
class PipelineConfig:
    project_prefix: str
    shots_root: Path
    shot_source_folder: Path
    xml_folder: Path
    key_template: Path
    base_shot_dirs: list
    cg_bg_groups: list
    cg_bg_passes: list
    cg_bg_variants: list
    cg_bg_versions: list

    @classmethod
    def from_yaml(cls, config_path):
        with open(config_path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        project = raw["project"]
        paths = raw["paths"]
        schema = raw["folder_schema"]
        return cls(
            project_prefix=project["prefix"],
            shots_root=Path(paths["shots_root"]),
            shot_source_folder=Path(paths["shot_source_folder"]),
            xml_folder=Path(paths["xml_folder"]),
            key_template=Path(paths["key_template"]),
            base_shot_dirs=schema["base_shot_dirs"],
            cg_bg_groups=schema["cg_bg_groups"],
            cg_bg_passes=schema["cg_bg_passes"],
            cg_bg_variants=schema["cg_bg_variants"],
            cg_bg_versions=schema["cg_bg_versions"],
        )


def _shot_token(filename):
    """Extract the ShNNNN[_LayerNN] token identifying which shot a frame belongs to."""
    shot_number = f"Sh{filename.split('_Sh')[1].split('_')[0]}"
    if "layer" in filename.lower():
        layer_number = f"_Layer{filename.split('_Layer')[1].split('_')[0]}"
        return shot_number + layer_number
    return shot_number


def reindex_shot_frames(shot_source_folder, shot_token):
    """Bump every frame of one shot's sequence up by one, so frame 0 becomes
    frame 1 and Nuke's 1-based frame range convention isn't broken. Frames
    are renamed through a temporary `BB` marker first to avoid collisions
    between a frame and the number it's about to become, and the connector
    before the frame number is normalized from `_` to `.` to match the
    naming convention used everywhere else in the pipeline."""
    for filename in reversed(os.listdir(shot_source_folder)):
        if shot_token not in filename:
            continue
        frame_token = filename.split("_")[-1]
        frame_number, extension = frame_token.rsplit(".", 1)
        bumped_frame = f"{int(frame_number) + 1:04}BB"
        new_filename = filename.replace(frame_token, f"{bumped_frame}.{extension}")
        last_underscore = new_filename.rindex("_")
        new_filename = f"{new_filename[:last_underscore]}.{new_filename[last_underscore + 1:]}"
        os.rename(
            os.path.join(shot_source_folder, filename),
            os.path.join(shot_source_folder, new_filename),
        )


def normalize_zero_indexed_sequences(shot_source_folder):
    """Detect any shot whose exported frame sequence starts at 0 and
    reindex it to start at 1."""
    frame_numbers = []
    for filename in os.listdir(shot_source_folder):
        if filename.count(".") < 2:
            try:
                frame_numbers.append(int(filename.split("_")[-1].split(".")[0]))
            except ValueError:
                continue

    if 0 in frame_numbers:
        for filename in reversed(os.listdir(shot_source_folder)):
            try:
                if int(filename.split("_")[-1].split(".")[0]) == 0:
                    reindex_shot_frames(shot_source_folder, _shot_token(filename))
            except ValueError:
                continue

    for filename in os.listdir(shot_source_folder):
        if "BB." in filename:
            os.rename(
                os.path.join(shot_source_folder, filename),
                os.path.join(shot_source_folder, filename.replace("BB.", ".")),
            )


def build_shot_folders(scene_name, shot_name, config):
    """Create the full per-shot folder tree if it doesn't already exist."""
    shot_root = config.shots_root / scene_name / shot_name
    if not shot_root.exists():
        for relative_dir in config.base_shot_dirs:
            (shot_root / relative_dir).mkdir(parents=True, exist_ok=True)
        for group in config.cg_bg_groups:
            for pass_name in config.cg_bg_passes:
                for variant in config.cg_bg_variants:
                    for version in config.cg_bg_versions:
                        (shot_root / "CG" / group / f"{pass_name}{variant}" / version).mkdir(
                            parents=True, exist_ok=True
                        )
    return shot_root


def copy_source_plates(shot_name, shot_source_folder, shot_root):
    """Copy any exported frames matching this shot into its PLATES/source folder."""
    destination_dir = shot_root / "PLATES" / "source"
    for filename in os.listdir(shot_source_folder):
        if shot_name not in filename:
            continue
        destination = destination_dir / filename
        if not destination.exists():
            shutil.copy(os.path.join(shot_source_folder, filename), destination)


def write_key_comp(scene_name, shot_name, duration, config):
    """Stamp a per-shot copy of the KEY Nuke template with this shot's
    output path, frame duration, and source-plate path. Expects a template
    containing the literal tokens X:/PROJECT_FILE.nk, _DURATION_, and
    X:/SOURCE_FILE.exr."""
    shot_root = config.shots_root / scene_name / shot_name
    output_path = shot_root / "Nuke" / "Live_Templates" / f"{shot_name}_KEY.nk"
    source_plates = shot_root / "PLATES" / "source" / f"{shot_name}.####.exr"

    template_text = config.key_template.read_text(encoding="utf-8")
    rendered = (
        template_text.replace("X:/PROJECT_FILE.nk", output_path.as_posix())
        .replace("_DURATION_", duration)
        .replace("X:/SOURCE_FILE.exr", source_plates.as_posix())
    )
    output_path.write_text(rendered, encoding="utf-8")


def _shot_duration(xml_path):
    """Read a shot's frame duration from its reference XML - the last <end>
    tag in the document, since shot_reference_generator.py writes the
    shot's own clip and its burn-in layer with matching end values."""
    root = ET.parse(str(xml_path)).getroot()
    return root.findall(".//end")[-1].text


def process_shot_xmls(config):
    """Walk every per-shot reference XML and build its folders + KEY comp."""
    for filename in os.listdir(config.xml_folder):
        if "ShotLayers_" in filename or config.project_prefix not in filename:
            continue
        xml_path = config.xml_folder / filename
        shot_name = filename.replace("_.xml", "").replace(".xml", "")
        scene_name = filename.split("_Sh")[0]
        duration = _shot_duration(xml_path)

        shot_root = build_shot_folders(scene_name, shot_name, config)
        copy_source_plates(shot_name, config.shot_source_folder, shot_root)
        write_key_comp(scene_name, shot_name, duration, config)
        LOGGER.info("Built %s/%s", scene_name, shot_name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="directory_creator_config.yaml", help="Path to config YAML")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    config = PipelineConfig.from_yaml(args.config)
    config.shots_root.mkdir(parents=True, exist_ok=True)
    normalize_zero_indexed_sequences(config.shot_source_folder)
    process_shot_xmls(config)
    LOGGER.info("Done. Shots built under %s", config.shots_root)


if __name__ == "__main__":
    main()
