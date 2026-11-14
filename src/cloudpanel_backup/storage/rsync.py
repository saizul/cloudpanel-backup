"""rsync over SSH (or to a local path) as a storage backend.

Uploads use ``rsync --mkpath`` (rsync 3.2.3 or newer on both ends), listing uses ``rsync --list-only`` and deletes run
``rm`` on the remote host over SSH.
"""

from __future__ import annotations

import re
import shlex
import tempfile
from pathlib import Path

from cloudpanel_backup.runner import CommandError, CommandRunner
from cloudpanel_backup.storage.base import ObjectInfo, StorageError, check_key

# -rw-r--r--          1,234 2026/11/14 02:30:01 cloudpanel/vps1/example.com/20261114T023000Z/manifest.json
_LIST_RE = re.compile(r"^(?P<mode>[-dlcbps][rwxsStT-]{9})\s+(?P<size>[\d,.]+)\s+\S+\s+\S+\s+(?P<name>.+)$")


def parse_target(target: str) -> tuple[str | None, str]:
    """``backup@203.0.113.5:/srv/backups`` -> ("backup@203.0.113.5", "/srv/backups"); local paths have no host."""
    if re.match(r"^[^/:]+:", target) and not target.startswith("rsync://"):
        host, _, path = target.partition(":")
        return host, path.rstrip("/") or "."
    return None, target.rstrip("/") or "/"


class RsyncStorage:
    def __init__(
        self,
        target: str,
        runner: CommandRunner,
        ssh_port: int | None = None,
        ssh_key: Path | None = None,
        extra_options: list[str] | None = None,
    ) -> None:
        self.target = target
        self.host, self.path = parse_target(target)
        self.runner = runner
        self.ssh_port = ssh_port
        self.ssh_key = ssh_key
        self.extra_options = list(extra_options or [])

    def describe(self) -> str:
        return f"rsync:{self.target}"

    def _ssh_args(self) -> list[str]:
        args = ["ssh", "-o", "BatchMode=yes"]
        if self.ssh_port:
            args += ["-p", str(self.ssh_port)]
        if self.ssh_key:
            args += ["-i", str(self.ssh_key)]
        return args

    def _rsync(self) -> list[str]:
        args = ["rsync", "--archive", "--partial", "--chmod=F600,D700", *self.extra_options]
        if self.host:
            args += ["-e", shlex.join(self._ssh_args())]
        return args

    def _remote(self, key: str) -> str:
        path = f"{self.path}/{check_key(key)}" if key else f"{self.path}/"
        return f"{self.host}:{path}" if self.host else path

    def put_file(self, local: Path, key: str, sha256: str | None = None) -> None:
        self.runner.run([*self._rsync(), "--mkpath", str(local), self._remote(key)])

    def put_bytes(self, data: bytes, key: str) -> None:
        with tempfile.TemporaryDirectory(prefix="cpb-") as tmp:
            path = Path(tmp) / Path(key).name
            path.write_bytes(data)
            self.put_file(path, key)

    def get_file(self, key: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.runner.run([*self._rsync(), self._remote(key), str(local)])
        except CommandError as exc:
            raise StorageError(f"could not fetch {key}: {exc}") from exc

    def get_bytes(self, key: str) -> bytes:
        with tempfile.TemporaryDirectory(prefix="cpb-") as tmp:
            path = Path(tmp) / "object"
            self.get_file(key, path)
            return path.read_bytes()

    def list(self, prefix: str) -> list[ObjectInfo]:
        prefix = prefix.strip("/")
        remote = self._remote(prefix) + "/" if prefix else self._remote("")
        try:
            result = self.runner.run([*self._rsync(), "--recursive", "--list-only", remote])
        except CommandError as exc:
            # rsync exits with 23 when the directory does not exist (no backups yet)
            if exc.returncode == 23:
                return []
            raise
        out = []
        for line in result.stdout.splitlines():
            m = _LIST_RE.match(line.strip())
            if not m or m.group("mode")[0] != "-":
                continue
            name = m.group("name")
            if name.startswith("./"):
                name = name[2:]
            key = f"{prefix}/{name}" if prefix else name
            size = int(re.sub(r"[,.]", "", m.group("size")))
            out.append(ObjectInfo(key, size))
        return sorted(out, key=lambda o: o.key)

    def delete(self, key: str) -> None:
        path = f"{self.path}/{check_key(key)}"
        if self.host:
            self.runner.run([*self._ssh_args(), self.host, "rm", "-f", "--", shlex.quote(path)])
        else:
            Path(path).unlink(missing_ok=True)

    def stat(self, key: str) -> ObjectInfo | None:
        parent = key.rsplit("/", 1)[0] if "/" in key else ""
        for info in self.list(parent):
            if info.key == key:
                return info
        return None
