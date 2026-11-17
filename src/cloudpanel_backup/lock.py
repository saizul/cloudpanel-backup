"""An exclusive run lock, so a timer run and a manual run never write to the same backup set at once.

The lock is an advisory ``flock`` on a small file (``<staging_dir>/cloudpanel-backup.lock`` by default). The kernel
releases it when the process exits, even after ``kill -9`` or a crash, so a stale lock file never blocks later runs.
The file holds a short JSON note (PID, host, command, start time) that is shown when another run is refused.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cloudpanel_backup.util import utcnow

log = logging.getLogger(__name__)

LOCK_NAME = "cloudpanel-backup.lock"


class LockError(RuntimeError):
    """Another cloudpanel-backup run holds the lock."""

    def __init__(self, path: Path, holder: dict[str, Any] | None, waited: float) -> None:
        self.path = path
        self.holder = holder or {}
        self.waited = waited
        super().__init__(self._message())

    def _message(self) -> str:
        who = describe_holder(self.holder)
        wait = f" after waiting {self.waited:.0f}s" if self.waited else ""
        return f"another cloudpanel-backup run holds {self.path}{wait}" + (f" ({who})" if who else "")


def describe_holder(holder: dict[str, Any]) -> str:
    parts = []
    if holder.get("command"):
        parts.append(str(holder["command"]))
    if holder.get("pid"):
        parts.append(f"pid {holder['pid']}")
    if holder.get("host"):
        parts.append(f"on {holder['host']}")
    if holder.get("started"):
        parts.append(f"since {holder['started']}")
    return ", ".join(parts)


def read_holder(path: Path) -> dict[str, Any] | None:
    """Return the note written by the current (or last) lock holder, or None if there is none or it is garbage."""
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "null")
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class RunLock:
    """Context manager around a non-blocking ``flock`` with an optional wait.

    ``timeout`` is how long to keep retrying (seconds); 0 means fail at once. ``sleep`` and ``clock`` are injectable
    so the tests do not have to wait.
    """

    def __init__(
        self,
        path: Path,
        command: str,
        host: str,
        timeout: float = 0.0,
        poll_interval: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout < 0:
            raise ValueError("lock timeout cannot be negative")
        self.path = path
        self.command = command
        self.host = host
        self.timeout = timeout
        self.poll_interval = max(poll_interval, 0.01)
        self._sleep = sleep
        self._clock = clock
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> RunLock:
        if self._fd is not None:
            raise RuntimeError("lock already held by this process")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # O_NOFOLLOW: refuse to follow a symlink planted in place of the lock file.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        started = self._clock()
        announced = False
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    waited = self._clock() - started
                    if waited >= self.timeout:
                        raise LockError(self.path, read_holder(self.path), waited) from None
                    if not announced:
                        announced = True
                        holder = describe_holder(read_holder(self.path) or {})
                        suffix = f" ({holder})" if holder else ""
                        log.info("waiting up to %.0fs for the run lock%s", self.timeout, suffix)
                    self._sleep(min(self.poll_interval, self.timeout - waited))
        except BaseException:
            os.close(fd)
            raise
        note = {
            "pid": os.getpid(),
            "host": self.host,
            "command": self.command,
            "started": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        os.ftruncate(fd, 0)
        os.pwrite(fd, (json.dumps(note) + "\n").encode("utf-8"), 0)
        self._fd = fd
        log.debug("acquired run lock %s", self.path)
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)  # leave an empty file behind; the next holder writes its own note
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        log.debug("released run lock %s", self.path)

    def __enter__(self) -> RunLock:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


def default_lock_path(staging_dir: Path) -> Path:
    return staging_dir / LOCK_NAME
