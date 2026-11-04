import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest

from handlecheck import update


def git(*args, cwd):
    identity = ["-c", "user.name=test", "-c", "user.email=test@example.com"]
    subprocess.run(["git", *identity, *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MARKER", str(tmp_path / "installed"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return home


def app_dir(home) -> Path:
    return home / ".local" / "lib" / "handlecheck" / "source"


def installs(tmp_path) -> list[str]:
    marker = tmp_path / "installed"
    return marker.read_text().split() if marker.exists() else []


INSTALLER = 'echo "$0" >> "$MARKER"\n'


@pytest.fixture
def clone(home, tmp_path):
    """An installed clone, and a second clone that can publish new commits."""
    origin = tmp_path / "origin.git"
    git("init", "--bare", "-b", "master", str(origin), cwd=tmp_path)
    work = tmp_path / "work"
    git("clone", str(origin), str(work), cwd=tmp_path)
    (work / "install.sh").write_text(INSTALLER)
    git("add", ".", cwd=work)
    git("commit", "-m", "one", cwd=work)
    git("push", "origin", "HEAD:master", cwd=work)
    app = app_dir(home)
    app.parent.mkdir(parents=True)
    git("clone", str(origin), str(app), cwd=tmp_path)
    return app, work


def publish(work):
    (work / "new.txt").write_text("new")
    git("add", ".", cwd=work)
    git("commit", "-m", "two", cwd=work)
    git("push", "origin", "HEAD:master", cwd=work)


def test_clone_is_pulled_and_reinstalled(clone, tmp_path):
    app, work = clone
    publish(work)
    assert update.update(app) == 0
    assert (app / "new.txt").exists()
    assert installs(tmp_path) == [str(app / "install.sh")]


def test_up_to_date_clone_is_left_alone(clone, tmp_path):
    app, _ = clone
    assert update.update(app) == 0
    assert installs(tmp_path) == []


@pytest.mark.parametrize("name", ["install.sh", "untracked.txt"])
def test_local_changes_stop_the_update(clone, tmp_path, capsys, name):
    app, work = clone
    publish(work)
    (app / name).write_text("mine")
    assert update.update(app) == 1
    assert name in capsys.readouterr().err
    assert (app / name).read_text() == "mine"
    assert not (app / "new.txt").exists()
    assert installs(tmp_path) == []


def test_diverged_clone_is_not_merged(clone, tmp_path):
    app, work = clone
    publish(work)
    (app / "mine.txt").write_text("mine")
    git("add", ".", cwd=app)
    git("commit", "-m", "mine", cwd=app)
    assert update.update(app) == 1
    assert not (app / "new.txt").exists()
    assert installs(tmp_path) == []


def fake_download(tmp_path):
    contents = tmp_path / "HandleCheck-master"
    contents.mkdir()
    (contents / "install.sh").write_text(INSTALLER)

    def download(destination):
        with tarfile.open(destination, "w:gz") as archive:
            archive.add(contents, arcname="HandleCheck-master")

    return download


def test_install_without_git_is_downloaded(home, tmp_path, monkeypatch):
    app = app_dir(home)
    app.mkdir(parents=True)
    monkeypatch.setattr(update, "_download", fake_download(tmp_path))
    assert update.update(app) == 0
    ran = installs(tmp_path)
    assert len(ran) == 1 and ran[0].endswith("/handlecheck/install.sh")
    assert not Path(ran[0]).exists()  # the temporary copy is cleaned up


def test_failed_download_changes_nothing(home, tmp_path, monkeypatch):
    app = app_dir(home)
    app.mkdir(parents=True)

    def fail(_destination):
        raise update.httpx.ConnectError("no network")

    monkeypatch.setattr(update, "_download", fail)
    assert update.update(app) == 1
    assert installs(tmp_path) == []


def test_other_copies_are_refused(home, tmp_path, capsys):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert update.update(elsewhere) == 1
    assert "install.sh" in capsys.readouterr().err


def test_running_check_blocks_the_update(clone, tmp_path, capsys):
    app, work = clone
    publish(work)
    held = update._lock_running_checks()
    try:
        # flock is per open file, so this second open sees the lock as taken.
        assert update.update(app) == 1
    finally:
        update.os.close(held)
    assert "a check is running" in capsys.readouterr().err
    assert not (app / "new.txt").exists()


def test_custom_site_list_is_mentioned(clone, home, tmp_path, capsys):
    app, work = clone
    publish(work)
    custom = home / ".local" / "share" / "handlecheck" / "sites.json"
    custom.parent.mkdir(parents=True)
    custom.write_text("{}")
    assert update.update(app) == 0
    assert str(custom) in capsys.readouterr().out
