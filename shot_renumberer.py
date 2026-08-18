"""Rename a shot's rendered/comp files, and rewrite the shot-code
references inside its own Nuke scripts, when a shot gets renumbered.

Shot numbers occasionally need to shift after the fact - a new shot
gets inserted where there wasn't room for one, for instance. This walks
a shot's own folder, renames every rendered/comp file (EXR, mov, mp4,
png) that carries the old shot code in its filename to the new one, and
separately rewrites the old code everywhere it appears inside the shot's
own .nk scripts - both in the script's own filename and in any node
inside it that references the old code by name (a Read node's file
knob, a burn-in label, and so on).

Raw camera source and reference footage are left alone - those files
usually predate the renumber and shouldn't be touched, so anything
sitting under a folder named for one of the skip markers below is
skipped, matching the same distinction directory_creator.py's config
draws between "source" (camera-original) and everything downstream of
it.

Not reproduced: the original also had a narrow substring fix-up for one
specific filename pattern it encountered (`_v01` -> `.1` inside a
version token, only under specific surrounding text). It wasn't possible
to verify what filenames that was actually targeting or confirm it
wouldn't misfire on a different naming pattern, so it's left out rather
than ported on a guess.
"""

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

LOGGER = logging.getLogger("shot_renumberer")


@dataclass
class RenumberConfig:
    renamed_extensions: list
    skip_folder_markers: list

    @classmethod
    def from_yaml(cls, config_path):
        with open(config_path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        return cls(
            renamed_extensions=list(raw["renamed_extensions"]),
            skip_folder_markers=list(raw["skip_folder_markers"]),
        )


def _is_skipped(path, skip_folder_markers):
    return any(marker in path.parts for marker in skip_folder_markers)


def find_files_to_rename(shot_root, old_code, extensions, skip_folder_markers):
    matches = []
    for extension in extensions:
        for path in shot_root.rglob(f"*{extension}"):
            if _is_skipped(path, skip_folder_markers):
                continue
            if old_code in path.name:
                matches.append(path)
    return matches


def rename_shot_files(shot_root, old_code, new_code, config):
    renamed = []
    for path in find_files_to_rename(shot_root, old_code, config.renamed_extensions, config.skip_folder_markers):
        new_path = path.with_name(path.name.replace(old_code, new_code))
        path.rename(new_path)
        renamed.append((path, new_path))
    return renamed


def rewrite_nuke_scripts(shot_root, old_code, new_code):
    """Rewrite old_code -> new_code in both a .nk script's own filename
    and its content, wherever either references the old shot code."""
    updated = []
    for path in shot_root.rglob("*.nk"):
        content = path.read_text(encoding="utf-8", errors="ignore")
        if old_code not in content and old_code not in path.name:
            continue
        new_content = content.replace(old_code, new_code)
        new_path = path.with_name(path.name.replace(old_code, new_code))
        new_path.write_text(new_content, encoding="utf-8")
        if new_path != path:
            path.unlink()
        updated.append((path, new_path))
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("shot_folder", help="Path to the shot's own folder, e.g. .../SEQ/SEQ_ShNNNN")
    parser.add_argument("old_code", help="Current shot code to replace")
    parser.add_argument("new_code", help="New shot code")
    parser.add_argument("--config", default="shot_renumberer_config.yaml", help="Path to config YAML")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    config = RenumberConfig.from_yaml(args.config)
    shot_root = Path(args.shot_folder)

    renamed = rename_shot_files(shot_root, args.old_code, args.new_code, config)
    for old_path, new_path in renamed:
        LOGGER.info("Renamed %s -> %s", old_path.name, new_path.name)

    updated_scripts = rewrite_nuke_scripts(shot_root, args.old_code, args.new_code)
    for old_path, new_path in updated_scripts:
        LOGGER.info("Updated Nuke script %s -> %s", old_path.name, new_path.name)

    LOGGER.info("Done. %d file(s) renamed, %d Nuke script(s) updated.", len(renamed), len(updated_scripts))


if __name__ == "__main__":
    main()
