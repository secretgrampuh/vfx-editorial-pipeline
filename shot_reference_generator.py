"""Split a full editorial timeline export into per-shot reference XMLs.

An editor cuts the whole movie in Premiere and exports the sequence as a
Final Cut Pro 7 XML. This tool reads that single file and, for each shot on
the timeline, writes a standalone Premiere sequence: one clip, pointing at
the real native camera file (not the offline proxy), padded with a few
frames of handle, with a burn-in graphic already spliced in. Vendors and
artists then work off those per-shot files instead of the full cut.

On the show this was built for, the follow-on step - batch-exporting all of
those per-shot XMLs through Adobe Media Encoder - couldn't hit the colorspace
the post-house needed for a batch export, so that step was done in DaVinci
Resolve instead. The XML-generation half here still holds up on its own.
"""

import argparse
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml
from lxml import etree as ET

LOGGER = logging.getLogger("shot_reference_generator")


@dataclass
class PipelineConfig:
    project_prefix: str
    scene_marker_track: int
    handle_frames: int
    number_step: int
    shot_numbers_template: Path
    transparent_element_template: Path
    burn_in_template: Path
    burn_in_graphic: Path
    output_dir: Path
    reference_output_dir: Path
    storage_hostname: str
    native_footage_root: str

    @classmethod
    def from_yaml(cls, config_path):
        with open(config_path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        project = raw["project"]
        paths = raw["paths"]
        storage = raw["storage"]
        template_dir = Path(paths["template_dir"])
        return cls(
            project_prefix=project["prefix"],
            scene_marker_track=int(project["scene_marker_track"]),
            handle_frames=int(project["handle_frames"]),
            number_step=int(project.get("number_step", 10)),
            shot_numbers_template=template_dir / paths["shot_numbers_template"],
            transparent_element_template=template_dir / paths["transparent_element_template"],
            burn_in_template=template_dir / paths["burn_in_template"],
            burn_in_graphic=Path(paths["burn_in_graphic"]),
            output_dir=Path(paths["output_dir"]),
            reference_output_dir=Path(paths["reference_output_dir"]),
            storage_hostname=storage["hostname"],
            native_footage_root=storage["native_footage_root"],
        )


def resolve_native_camera_path(offline_path, native_footage_root):
    """Rewrite an offline/proxy clip path to the real native camera file.

    Native camera media follows a fixed convention: <root>/<native_footage_root>
    /<reel>/XDROOT/Clip/<FILENAME>.MXF, where <reel> is the first 4 characters
    of the filename and XDROOT/Clip is the folder layout Sony XDCAM camera
    media is delivered in.
    """
    filename = os.path.basename(offline_path)
    reel_code = filename[:4]
    native_filename = filename.upper().replace(".MOV", ".MXF")
    storage_root = offline_path.split("FOOTAGE")[0]
    return f"{storage_root}{native_footage_root}/{reel_code}/XDROOT/Clip/{native_filename}"


def _element_path(tree, element):
    return tree.getpath(element).replace("/xmeml", "./")


def _resolve_boundary_time(tree, root, neighbor, tag):
    """Look up a start/end value from a neighboring clip.

    FCP7 XML uses -1 as a sentinel meaning "this clip's timing is defined by
    the transition on the adjacent clip" - this pulls the real value from
    that neighbor instead.
    """
    xpath = _element_path(tree, neighbor) + f"/{tag}"
    return root.find(xpath).text


def _resolve_transition_sentinels(time_pairs):
    """Fill in -1 start/end sentinels from the adjacent clip and drop the
    now-redundant neighbor entry, so every clip has a real start/end."""
    last_index = len(time_pairs) - 1
    for index, pair in enumerate(time_pairs):
        if pair[0] == "-1":
            previous_pair = time_pairs[index - 1]
            pair[0] = previous_pair[0]
            previous_pair[0] = "remove"
        if pair[1] == "-1" and index != last_index:
            next_pair = time_pairs[index + 1]
            pair[1] = next_pair[1]
            next_pair[1] = "remove"
    return [pair for pair in time_pairs if "remove" not in pair]


def _collapse_nested_clips(clip_ranges):
    """Drop any clip range that sits entirely inside another clip's range,
    flattening a multi-track edit into one non-overlapping shot list."""
    nested = [
        outer
        for outer in clip_ranges
        for other in clip_ranges
        if int(outer[0]) > int(other[0]) and int(outer[1]) < int(other[1])
    ]
    return [clip for clip in clip_ranges if clip not in nested]


def _deduplicate_preserving_order(items):
    unique = []
    for item in items:
        if item not in unique:
            unique.append(item)
    return unique


def _get_clip_filepath(root, clipitem_path_root):
    """Read a clip's source path, falling back to a shared <file> node.

    FCP7 XML sometimes has several clipitems reference the same <file> node
    by id instead of each getting its own - when the direct pathurl lookup
    comes up empty, this locates the shared node instead.
    """
    pathurl_path = f"{clipitem_path_root}/file/pathurl"
    element = root.find(pathurl_path)
    if element is not None and element.text:
        return pathurl_path, element.text
    file_id = root.find(f"{clipitem_path_root}/file").get("id")
    shared_pathurl = root.xpath(f"//file[@id='{file_id}']/pathurl/text()")
    return pathurl_path, shared_pathurl[0] if shared_pathurl else None


def build_shot_label_track(shots, config, run_stamp):
    """Build a burn-in "shot number" layer covering every shot, alternating
    across two video tracks so adjacent labels don't overlap."""
    tree = ET.parse(str(config.shot_numbers_template))
    root = tree.getroot()
    track_1_marker = root.find(".//video/track[1]/enabled")
    track_2_marker = root.find(".//video/track[2]/enabled")

    for index, shot in enumerate(shots):
        temp_copy = config.output_dir / f"temp_{run_stamp}_{index}.xml"
        shutil.copy(config.transparent_element_template, temp_copy)
        subprocess.call(["sed", "-i", "-e", f"s/SHOT_NAME/{shot[0]}/g", str(temp_copy)])
        subprocess.call(["sed", "-i", "-e", f"s/START_POINT/{shot[1]}/g", str(temp_copy)])
        subprocess.call(["sed", "-i", "-e", f"s/END_POINT/{shot[2]}/g", str(temp_copy)])

        clip_tree = ET.parse(str(temp_copy))
        clip_element = clip_tree.getroot().find(".//clipitem")
        clip_xml = ET.XML(ET.tostring(clip_element))

        previous_shot = shots[index - 1] if index > 0 else None
        if previous_shot is not None and int(shot[1]) < int(previous_shot[2]):
            track_2_marker.addprevious(clip_xml)
        else:
            track_1_marker.addprevious(clip_xml)
        temp_copy.unlink()

    output_file = config.output_dir / f"ShotLayers_{run_stamp}.xml"
    tree.write(str(output_file))


def write_shot_reference_xmls(timeline_xml_path, shots, config):
    """Write one standalone reference XML per shot: its clip(s) only, native
    camera path, handle-padded in/out, and a burn-in graphic spliced in."""
    for shot in shots:
        output_file = config.reference_output_dir / f"{shot[0]}.xml"
        tree = ET.parse(str(timeline_xml_path))
        root = tree.getroot()
        duration = None

        for clipitem in reversed(root.findall(".//video/track/clipitem")):
            clipitem_path = _element_path(tree, clipitem)
            scene_start = root.find(f"{clipitem_path}/start").text
            scene_end = root.find(f"{clipitem_path}/end").text
            scene_in = root.find(f"{clipitem_path}/in").text
            scene_out = root.find(f"{clipitem_path}/out").text

            pathurl_path, clip_filepath = _get_clip_filepath(root, clipitem_path)
            if clip_filepath:
                native_path = resolve_native_camera_path(clip_filepath, config.native_footage_root)
                pathurl_element = root.find(pathurl_path)
                if pathurl_element is not None:
                    pathurl_element.text = native_path
                else:
                    file_id = root.find(f"{clipitem_path}/file").get("id")
                    shared_file = root.xpath(
                        f".//file[@id='{file_id}']/pathurl/ancestor-or-self::file"
                    )
                    if shared_file:
                        target_node = root.find(f"{clipitem_path}/file")
                        for child in shared_file[0].getchildren():
                            if child.text and "file://" in child.text:
                                child.text = resolve_native_camera_path(
                                    child.text, config.native_footage_root
                                )
                            target_node.append(child)

            if scene_start == "-1":
                scene_start = _resolve_boundary_time(tree, root, clipitem.getprevious(), "start")
            if scene_end == "-1":
                scene_end = _resolve_boundary_time(tree, root, clipitem.getnext(), "end")

            if int(scene_start) >= int(shot[1]) and int(scene_end) <= int(shot[2]):
                new_in = str(int(scene_in) - config.handle_frames)
                new_out = str(int(scene_out) + config.handle_frames)
                root.find(f"{clipitem_path}/in").text = new_in
                root.find(f"{clipitem_path}/out").text = new_out
                root.find(".//sequence/name").text = shot[0]
                root.find(f"{clipitem_path}/start").text = "0"
                duration = str(int(new_out) - int(new_in))
                root.find(f"{clipitem_path}/end").text = duration
            else:
                clipitem.getparent().remove(clipitem)

        # `duration` is set from the last matching clip in this reversed
        # pass - correct as long as exactly one clip per shot matches, which
        # holds unless a shot spans more than one overlapping layer clip.
        burn_in_tree = ET.parse(str(config.burn_in_template))
        burn_in_pathurl = burn_in_tree.getroot().find(".//file/pathurl")
        burn_in_pathurl.text = f"file://localhost{Path(config.burn_in_graphic).resolve().as_posix()}"
        burn_in_track = burn_in_tree.getroot().find(".//video/track")
        root.find(".//video").append(burn_in_track)

        burn_in_clip = root.find(".//clipitem[@id='clipitem-999999']")
        burn_in_path = _element_path(tree, burn_in_clip)
        root.find(f"{burn_in_path}/end").text = duration
        burn_in_in = root.find(f"{burn_in_path}/in").text
        root.find(f"{burn_in_path}/out").text = str(int(burn_in_in) + int(duration))

        tree.write(str(output_file))


def split_multilayer_shots(config):
    """Explode any shot reference XML that ended up with more than one clip
    (overlapping layers, e.g. a screen composited inside another shot) into
    one file per layer, since Nuke/AE need each as a separate element. The
    burn-in graphic clip spliced in by write_shot_reference_xmls isn't a
    real layer and is skipped."""
    raw_output_dir = config.reference_output_dir / "Raw_XML"
    raw_output_dir.mkdir(parents=True, exist_ok=True)

    for shot_file in config.reference_output_dir.iterdir():
        if shot_file.name.startswith(".") or not shot_file.is_file():
            continue
        source_clips = ET.parse(str(shot_file)).getroot().findall(".//video//clipitem")
        for layer_index, source_clip in enumerate(source_clips):
            file_name = source_clip.find(".//file/name")
            if file_name is not None and file_name.text == "BurnIn.png":
                continue

            layer_label = f"_Layer{layer_index + 1:02}"
            layer_file = raw_output_dir / shot_file.name.replace(".xml", f"{layer_label}.xml")
            layer_tree = ET.parse(str(shot_file))
            layer_root = layer_tree.getroot()
            layer_clips = layer_root.findall(".//video//clipitem")
            node_to_keep = layer_tree.getpath(layer_clips[layer_index])
            for clip in layer_clips:
                if layer_tree.getpath(clip) != node_to_keep:
                    clip.getparent().remove(clip)
            for filter_node in layer_root.findall(".//filter"):
                filter_node.getparent().remove(filter_node)
            for name_element in layer_root.findall(".//name"):
                name_element.text = layer_file.stem
            layer_tree.write(str(layer_file))


def clean_local_host_paths(directory):
    """Strip the `file://localhost` prefix and collapse doubled slashes that
    Premiere adds back onto paths on re-export."""
    for entry in Path(directory).iterdir():
        if entry.suffix != ".xml":
            continue
        subprocess.call(["sed", "-i", "-e", "s,localhost,,g", str(entry)])
        subprocess.call(["sed", "-i", "-e", "s,///,//,g", str(entry)])


def ingest_editorial_timeline(timeline_xml_path, config, run_stamp):
    """Read the full timeline export, derive the shot list, and write the
    shot-label track and per-shot reference XMLs."""
    tree = ET.parse(str(timeline_xml_path))
    root = tree.getroot()

    scenes = []
    scene_track = root.find(f".//video/track[{config.scene_marker_track}]")
    for scene_clip in scene_track.findall("./clipitem"):
        clip_path = _element_path(tree, scene_clip)
        scene_name = root.find(f"{clip_path}/name").text.upper()[0:5]
        scene_start = root.find(f"{clip_path}/start").text
        scene_end = root.find(f"{clip_path}/end").text
        scenes.append([scene_name, scene_start, scene_end])
    scene_track.getparent().remove(scene_track)

    for index, scene in enumerate(scenes):
        scene[0] = f"{config.project_prefix}_{(index + 1) * config.number_step:03}_{scene[0]}_"

    starts = [c.text for c in root.findall(".//video//start") if "item" in tree.getpath(c)]
    ends = [c.text for c in root.findall(".//video//end") if "item" in tree.getpath(c)]
    time_pairs = [list(pair) for pair in zip(starts, ends)]
    time_pairs = _resolve_transition_sentinels(time_pairs)
    non_overlapping_clips = _collapse_nested_clips(time_pairs)
    non_overlapping_clips = _deduplicate_preserving_order(non_overlapping_clips)

    # Shot numbers restart at config.number_step for every new scene (not a
    # single count across the whole movie) and climb by config.number_step
    # rather than by 1, matching the studio convention of leaving numbering
    # gaps a scene/shot can later be inserted into without renumbering
    # anything downstream (e.g. a pickup shot landing as Sh0015 between
    # Sh0010 and Sh0020). Clips outside any scene marker's range keep a
    # single un-prefixed running count as a fallback.
    scene_shot_counts = {}
    fallback_count = 0
    shots = []
    for clip in non_overlapping_clips:
        matching_scene = next(
            (
                scene
                for scene in scenes
                if int(clip[0]) >= int(scene[1]) and int(clip[1]) <= int(scene[2])
            ),
            None,
        )
        if matching_scene is not None:
            scene_shot_counts[matching_scene[0]] = (
                scene_shot_counts.get(matching_scene[0], 0) + config.number_step
            )
            shot_label = f"{matching_scene[0]}Sh{scene_shot_counts[matching_scene[0]]:04}"
        else:
            fallback_count += config.number_step
            shot_label = f"Sh{fallback_count:04}"
        shots.append([shot_label, clip[0], clip[1]])
    LOGGER.info("Resolved %d shots from timeline", len(shots))

    for clipitem in root.findall(".//video//clipitem"):
        clipitem_path = _element_path(tree, clipitem)
        pathurl_path, clip_filepath = _get_clip_filepath(root, clipitem_path)
        if not clip_filepath:
            continue
        clip_filepath = clip_filepath.replace(
            f"file://localhost//{config.storage_hostname}", f"file://{config.storage_hostname}"
        )
        clip_filepath = clip_filepath.replace(
            f"file://localhost/{config.storage_hostname}", f"file://{config.storage_hostname}"
        )
        native_path = resolve_native_camera_path(clip_filepath, config.native_footage_root)
        pathurl_element = root.find(pathurl_path)
        if pathurl_element is not None:
            pathurl_element.text = native_path
        else:
            file_id = root.find(f"{clipitem_path}/file").get("id")
            shared_node = root.xpath(f"//file[@id='{file_id}']/pathurl")[0]
            root.find(_element_path(tree, shared_node)).text = native_path

    build_shot_label_track(shots, config, run_stamp)
    write_shot_reference_xmls(timeline_xml_path, shots, config)
    return shots


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-xml", required=True, help="Full timeline XML exported from Premiere")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    config = PipelineConfig.from_yaml(args.config)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    config.reference_output_dir.mkdir(parents=True, exist_ok=True)
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    ingest_editorial_timeline(args.input_xml, config, run_stamp)
    clean_local_host_paths(config.reference_output_dir)
    clean_local_host_paths(config.output_dir)
    split_multilayer_shots(config)
    LOGGER.info("Done. Reference XMLs written to %s", config.reference_output_dir)


if __name__ == "__main__":
    main()
