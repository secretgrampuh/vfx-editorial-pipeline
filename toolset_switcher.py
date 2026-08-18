"""Switch which show's Nuke toolset an artist's local init.py loads.

Multiple shows shared artist workstations, and each had its own custom
Nuke toolset (gizmos, plugins, menu items) loaded via a single
nuke.pluginAddPath() call in the artist's personal ~/.nuke/init.py. Since
only one path can be active at a time, switching shows meant hand-editing
that file - this does it with one command instead.

The original version of this was three separate scripts, one per show,
each hardcoding which of the *other* toolset path strings to search for
and replace. That's an N*(N-1) problem in disguise: every time a new show
got added, every existing script needed a matching update, and it was
easy to get wrong - one of the three ended up broken, silently calling an
unrelated script instead of doing the swap. This generalizes it instead:
given the full set of known toolset paths, it replaces whichever one is
currently active with the requested one, or adds the line fresh if none
is active yet (the original scripts had no cold-start handling at all).

Note: on the show this was built for, a second, unrelated mechanism
(NUKE_PATH + PATH environment variables, set in a launcher script that
starts Nuke directly) was used to load one show's proprietary compiled
rotoscope plugin. That's a fine pattern in its own right - scoping the
env vars to the launched process instead of permanently rewriting a
shared config file - but it's plugin-specific, not a toolset-switching
problem, so it isn't reproduced here. A launcher for it would look like:

    set NUKE_PATH=/path/to/YourPlugin/Latest
    set PATH=%PATH%;/path/to/YourPlugin/Latest/shared_binaries
    "C:/Program Files/Nuke.../Nuke.exe" --nuke
"""

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

LOGGER = logging.getLogger("toolset_switcher")


@dataclass
class PipelineConfig:
    toolsets: dict
    nuke_init_path: Path

    @classmethod
    def from_yaml(cls, config_path):
        with open(config_path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        init_path = raw.get("nuke_init_path")
        return cls(
            toolsets=raw["toolsets"],
            nuke_init_path=Path(init_path) if init_path else Path.home() / ".nuke" / "init.py",
        )


def _toolset_line(toolset_path):
    return f'nuke.pluginAddPath("{toolset_path}")\n'


def swap_toolset_line(lines, known_toolset_paths, target_path):
    """Replace whichever known toolset path is active with target_path.
    Adds a fresh line at the top if none of the known paths are present."""
    new_lines = []
    replaced = False
    for line in lines:
        if any(path in line for path in known_toolset_paths):
            new_lines.append(_toolset_line(target_path))
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.insert(0, _toolset_line(target_path))
    return new_lines


def switch_toolset(toolset_name, config):
    if toolset_name not in config.toolsets:
        known = ", ".join(config.toolsets)
        raise ValueError(f"Unknown toolset '{toolset_name}'. Known toolsets: {known}")

    target_path = config.toolsets[toolset_name]
    init_path = config.nuke_init_path
    if not init_path.exists():
        raise FileNotFoundError(f"Nuke init.py not found at {init_path}")

    lines = init_path.read_text(encoding="utf-8").splitlines(keepends=True)
    new_lines = swap_toolset_line(lines, list(config.toolsets.values()), target_path)
    init_path.write_text("".join(new_lines), encoding="utf-8")
    LOGGER.info("Switched Nuke toolset to '%s' (%s)", toolset_name, target_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("toolset", help="Name of the toolset to switch to, e.g. tko, mmr, experimental")
    parser.add_argument("--config", default="toolset_switcher_config.yaml", help="Path to config YAML")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    config = PipelineConfig.from_yaml(args.config)
    switch_toolset(args.toolset, config)


if __name__ == "__main__":
    main()
