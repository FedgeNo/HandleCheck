"""Where handlecheck keeps its data, following the XDG base directory spec."""

from __future__ import annotations

import os
from pathlib import Path

APP = "handlecheck"
SITES_NAME = "sites.json"
BUNDLED_SITES = Path(__file__).with_name(SITES_NAME)


def data_home() -> Path:
    """This user's own data, such as a site list they have calibrated."""
    base = os.environ.get("XDG_DATA_HOME", "")
    # The spec says relative paths are invalid and must be ignored.
    root = Path(base) if os.path.isabs(base) else Path.home() / ".local" / "share"
    return root / APP


def data_dirs() -> list[Path]:
    """Data shared by all users, most important first."""
    dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [Path(d) / APP for d in dirs.split(":") if os.path.isabs(d)]


def sites_file() -> Path:
    """The site list to use, falling back to the one shipped with the code."""
    for directory in [data_home(), *data_dirs()]:
        if (directory / SITES_NAME).is_file():
            return directory / SITES_NAME
    return BUNDLED_SITES
