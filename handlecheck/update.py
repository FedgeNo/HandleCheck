"""Update an installed copy to the latest code.

A git clone is pulled. Any other install is replaced with a fresh download.
Either way the code's own install.sh then finishes the job.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx

from . import paths

ARCHIVE = "https://github.com/FedgeNo/HandleCheck/archive/refs/heads/master.tar.gz"
ROOT = Path(__file__).resolve().parents[1]


def _fail(message: str) -> int:
    print(f"handlecheck: {message}", file=sys.stderr)
    return 1


def _lock_running_checks() -> int | None:
    """Take the lock browser runs hold, or return None if one is running."""
    path = os.path.join(tempfile.gettempdir(), f"handlecheck.{os.getuid()}.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _install(source: Path) -> int:
    # The current directory may be inside the code that's about to be replaced.
    return subprocess.run(["bash", str(source / "install.sh")], cwd=Path.home()).returncode


def _update_clone(root: Path) -> tuple[int, bool]:
    """Return an exit code and whether the code changed."""

    def git(*args: str, capture: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=capture, text=True)

    try:
        changes = git("status", "--porcelain", capture=True)
    except FileNotFoundError:
        return _fail(f"{root} is a git repository, but git isn't installed"), False
    if changes.returncode:
        return _fail(changes.stderr.strip() or "git status failed"), False
    if changes.stdout.strip():
        print(changes.stdout, end="", file=sys.stderr)
        return _fail(f"not updating: commit or discard the changes above in {root}"), False

    before = git("rev-parse", "HEAD", capture=True).stdout
    pulled = git("pull", "--ff-only")
    if pulled.returncode:
        return _fail("git pull failed; nothing was changed"), False
    if git("rev-parse", "HEAD", capture=True).stdout == before:
        return 0, False
    return _install(root), True


def _download(destination: Path) -> None:
    with httpx.stream("GET", ARCHIVE, follow_redirects=True, timeout=60) as response:
        response.raise_for_status()
        with destination.open("wb") as archive:
            for chunk in response.iter_bytes():
                archive.write(chunk)


def _update_download() -> int:
    with tempfile.TemporaryDirectory(prefix="handlecheck-update-") as temp:
        archive = Path(temp) / "latest.tar.gz"
        # Not the temporary directory itself: the installer copies this
        # directory's permissions, and that one is private.
        source = Path(temp) / "handlecheck"
        source.mkdir()
        print(f"Downloading {ARCHIVE}", flush=True)
        try:
            _download(archive)
        except httpx.HTTPError as error:
            return _fail(f"download failed; nothing was changed: {error}")
        unpacked = subprocess.run(
            ["tar", "-xzf", str(archive), "-C", str(source), "--strip-components=1", "--no-same-owner"]
        )
        if unpacked.returncode or not (source / "install.sh").is_file():
            return _fail("the download wasn't usable; nothing was changed")
        return _install(source)


def update(root: Path = ROOT) -> int:
    installed = Path.home() / ".local" / "lib" / paths.APP / "source"
    if root != installed.resolve():
        return _fail(f"{root} wasn't set up by install.sh, so update it the way you installed it")

    lock = _lock_running_checks()
    if lock is None:
        return _fail("a check is running; update when it has finished")
    try:
        if (root / ".git").exists():
            code, changed = _update_clone(root)
        else:
            code, changed = _update_download(), True
    finally:
        os.close(lock)

    custom = paths.data_home() / paths.SITES_NAME
    if code == 0 and changed and custom.is_file():
        print(f"Your site list still takes priority over the updated one: {custom}")
    return code
