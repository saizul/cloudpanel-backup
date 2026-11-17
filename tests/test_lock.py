import json
import os
from pathlib import Path

import pytest

from cloudpanel_backup.config import ConfigError, parse_config
from cloudpanel_backup.lock import LockError, RunLock, describe_holder, read_holder
from test_cli import cli, write_config


class FakeClock:
    """A monotonic clock that only moves when the lock sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_lock_writes_holder_note_and_clears_it(tmp_path: Path):
    path = tmp_path / "staging" / "run.lock"
    with RunLock(path, "backup", "vps1") as lock:
        assert lock.held
        note = read_holder(path)
        assert note is not None
        assert note["pid"] == os.getpid() and note["command"] == "backup" and note["host"] == "vps1"
        assert note["started"].endswith("Z")
        assert path.stat().st_mode & 0o777 == 0o600
    assert not lock.held
    assert path.read_text() == ""
    assert read_holder(path) is None


def test_second_lock_is_refused_with_holder_details(tmp_path: Path):
    path = tmp_path / "run.lock"
    with RunLock(path, "backup", "vps1"), pytest.raises(LockError) as exc:
        RunLock(path, "prune", "vps1").acquire()
    message = str(exc.value)
    assert "another cloudpanel-backup run holds" in message
    assert "backup" in message and f"pid {os.getpid()}" in message
    assert exc.value.holder["command"] == "backup"
    # released: the next run gets it
    with RunLock(path, "prune", "vps1"):
        pass


def test_lock_waits_up_to_the_timeout(tmp_path: Path):
    path = tmp_path / "run.lock"
    clock = FakeClock()
    with RunLock(path, "backup", "vps1"):
        waiter = RunLock(path, "backup", "vps1", timeout=5, poll_interval=2, sleep=clock.sleep, clock=clock)
        with pytest.raises(LockError) as exc:
            waiter.acquire()
    assert clock.sleeps == [2, 2, 1]
    assert exc.value.waited == 5
    assert "after waiting 5s" in str(exc.value)


def test_lock_acquired_once_holder_releases(tmp_path: Path):
    path = tmp_path / "run.lock"
    holder = RunLock(path, "backup", "vps1").acquire()

    def sleep(seconds: float) -> None:
        holder.release()  # the other run finishes while we wait

    waiter = RunLock(path, "restore", "vps1", timeout=60, sleep=sleep, clock=lambda: 0.0)
    with waiter:
        assert read_holder(path)["command"] == "restore"


def test_stale_lock_file_does_not_block(tmp_path: Path):
    path = tmp_path / "run.lock"
    path.write_text(json.dumps({"pid": 999999, "command": "backup", "started": "2020-01-01T00:00:00Z"}))
    with RunLock(path, "backup", "vps1"):
        assert read_holder(path)["pid"] == os.getpid()


def test_lock_refuses_symlink(tmp_path: Path):
    target = tmp_path / "victim"
    target.write_text("keep me")
    link = tmp_path / "run.lock"
    link.symlink_to(target)
    with pytest.raises(OSError):
        RunLock(link, "backup", "vps1").acquire()
    assert target.read_text() == "keep me"


def test_lock_validation_and_helpers(tmp_path: Path):
    with pytest.raises(ValueError):
        RunLock(tmp_path / "x", "backup", "h", timeout=-1)
    lock = RunLock(tmp_path / "x", "backup", "h").acquire()
    with pytest.raises(RuntimeError):
        lock.acquire()
    lock.release()
    lock.release()  # idempotent
    assert describe_holder({}) == ""
    assert describe_holder({"command": "prune", "pid": 7}) == "prune, pid 7"
    (tmp_path / "garbage").write_text("[1, 2]")
    assert read_holder(tmp_path / "garbage") is None
    assert read_holder(tmp_path / "missing") is None


def test_lock_config():
    cfg = parse_config({"backup": {"staging_dir": "/srv/stage", "lock_timeout": 30}})
    assert cfg.backup.lock_path() == Path("/srv/stage/cloudpanel-backup.lock")
    assert cfg.backup.lock_timeout == 30
    cfg = parse_config({"backup": {"lock_file": "/run/cpb.lock"}})
    assert cfg.backup.lock_path() == Path("/run/cpb.lock")
    with pytest.raises(ConfigError):
        parse_config({"backup": {"lock_timeout": -1}})
    with pytest.raises(ConfigError):
        parse_config({"backup": {"lock_timeout": "10"}})


def test_cli_refuses_overlapping_runs(server):
    cfg = write_config(server)
    base = ["--config", str(cfg)]
    lock_path = server["root"] / "staging" / "cloudpanel-backup.lock"
    with RunLock(lock_path, "backup", "vps1"):
        code, out = cli([*base, "backup"])
        assert code == 3
        assert not (server["root"] / "store").exists()
        assert cli([*base, "prune"])[0] == 3
        # read-only commands and dry runs do not need the lock
        assert cli([*base, "backup", "--dry-run"])[0] == 0
        assert cli([*base, "prune", "--dry-run"])[0] == 0
        assert cli([*base, "list"])[0] == 0
        # --no-lock is an explicit override
        code, out = cli([*base, "--no-lock", "backup", "--site", "shop.example.com"])
        assert code == 0, out
        assert cli([*base, "restore", "shop.example.com", "--target", str(server["root"] / "r"), "--no-chown"])[0] == 3
    code, out = cli([*base, "backup"])
    assert code == 0, out
    assert lock_path.exists() and lock_path.read_text() == ""


def test_cli_lock_timeout_option(server, capsys):
    cfg = write_config(server)
    with RunLock(server["root"] / "staging" / "cloudpanel-backup.lock", "backup", "vps1"):
        code, _ = cli(["--config", str(cfg), "--lock-timeout", "0", "prune"])
    assert code == 3
    assert "another cloudpanel-backup run holds" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli(["--lock-timeout", "-5", "prune"])
    with pytest.raises(SystemExit):
        cli(["--lock-timeout", "soon", "prune"])


def test_cli_custom_lock_file(server, tmp_path):
    lock_path = tmp_path / "locks" / "cpb.lock"
    cfg = write_config(server, extra="")
    text = cfg.read_text().replace("[backup]\n", f'[backup]\nlock_file = "{lock_path}"\n', 1)
    cfg.write_text(text)
    with RunLock(lock_path, "backup", "vps1"):
        assert cli(["--config", str(cfg), "backup"])[0] == 3
    assert cli(["--config", str(cfg), "backup"])[0] == 0
