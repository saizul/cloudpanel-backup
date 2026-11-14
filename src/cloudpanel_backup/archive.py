"""Create and safely extract the file archive of a site (tar + gzip)."""

from __future__ import annotations

import fnmatch
import os
import stat
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class UnsafeArchiveError(ValueError):
    """The archive contains a member that would land outside the target directory."""


@dataclass
class ArchiveStats:
    files: int = 0
    directories: int = 0
    symlinks: int = 0
    excluded: int = 0
    skipped_special: int = 0
    bytes_in: int = 0


class ExcludeMatcher:
    """Matches paths relative to the site directory.

    - a pattern without ``/`` (``node_modules``, ``*.log``) matches any path component with that name
    - a pattern with ``/`` (``wp-content/cache``) matches that relative path and everything below it;
      a leading ``/`` anchors it to the site directory, otherwise it may appear at any depth
    """

    def __init__(self, patterns: Iterable[str]) -> None:
        self.name_patterns: list[str] = []
        self.path_patterns: list[tuple[str, bool]] = []
        for raw in patterns:
            pattern = raw.strip().rstrip("/")
            if not pattern:
                continue
            if "/" in pattern:
                anchored = pattern.startswith("/")
                self.path_patterns.append((pattern.lstrip("/"), anchored))
            else:
                self.name_patterns.append(pattern)

    def matches(self, rel_path: str) -> bool:
        parts = PurePosixPath(rel_path).parts
        if any(fnmatch.fnmatchcase(part, pat) for part in parts for pat in self.name_patterns):
            return True
        for pattern, anchored in self.path_patterns:
            depth = pattern.count("/") + 1
            starts = [0] if anchored else range(len(parts) - depth + 1)
            for start in starts:
                candidate = "/".join(parts[start : start + depth])
                if len(parts) >= start + depth and fnmatch.fnmatchcase(candidate, pattern):
                    return True
        return False


def create_archive(
    source: Path,
    output: Path,
    excludes: Iterable[str] = (),
    compression_level: int = 6,
    arcname: str = "site",
) -> ArchiveStats:
    """Write ``source`` to ``output`` as a gzipped tar with every member below ``arcname/``.

    Symlinks are stored as links (not followed); sockets, FIFOs and device files are skipped.
    """
    matcher = ExcludeMatcher(excludes)
    stats = ArchiveStats()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz", compresslevel=compression_level, format=tarfile.PAX_FORMAT) as tar:
        tar.add(source, arcname=arcname, recursive=False)
        for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
            rel_dir = os.path.relpath(dirpath, source)
            rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
            kept_dirs = []
            for name in sorted(dirnames):
                rel = f"{rel_dir}/{name}" if rel_dir else name
                if matcher.matches(rel):
                    stats.excluded += 1
                    continue
                full = Path(dirpath, name)
                if full.is_symlink():
                    tar.add(full, arcname=f"{arcname}/{rel}", recursive=False)
                    stats.symlinks += 1
                    continue
                kept_dirs.append(name)
                tar.add(full, arcname=f"{arcname}/{rel}", recursive=False)
                stats.directories += 1
            dirnames[:] = kept_dirs
            for name in sorted(filenames):
                rel = f"{rel_dir}/{name}" if rel_dir else name
                if matcher.matches(rel):
                    stats.excluded += 1
                    continue
                full = Path(dirpath, name)
                try:
                    mode = full.lstat().st_mode
                except FileNotFoundError:
                    continue  # removed while we were walking
                if stat.S_ISLNK(mode):
                    stats.symlinks += 1
                elif stat.S_ISREG(mode):
                    stats.files += 1
                    stats.bytes_in += full.lstat().st_size
                else:
                    stats.skipped_special += 1
                    continue
                try:
                    tar.add(full, arcname=f"{arcname}/{rel}", recursive=False)
                except (FileNotFoundError, PermissionError):
                    stats.skipped_special += 1
    return stats


def _check_member(member: tarfile.TarInfo, target: Path, strip: str) -> str | None:
    name = member.name
    if name == strip:
        return None
    if not name.startswith(strip + "/"):
        raise UnsafeArchiveError(f"unexpected member outside {strip}/: {name}")
    rel = name[len(strip) + 1 :]
    path = PurePosixPath(rel)
    if path.is_absolute() or ".." in path.parts or not rel:
        raise UnsafeArchiveError(f"unsafe path in archive: {name}")
    if member.isdev() or member.isfifo():
        raise UnsafeArchiveError(f"device or FIFO in archive: {name}")
    if member.issym() and not PurePosixPath(member.linkname).is_absolute():
        # Absolute links (to shared folders, for example) are restored as they were; they cannot be used to write
        # outside the target because members below a symlink are refused in extract_archive().
        resolved = os.path.normpath(os.path.join(str(target), os.path.dirname(rel), member.linkname))
        if resolved != str(target) and not resolved.startswith(str(target) + os.sep):
            raise UnsafeArchiveError(f"symlink escapes the target: {name} -> {member.linkname}")
    if member.islnk():
        link = PurePosixPath(member.linkname)
        if not member.linkname.startswith(strip + "/") or ".." in link.parts:
            raise UnsafeArchiveError(f"hard link escapes the target: {name} -> {member.linkname}")
    return rel


def list_archive(path: Path) -> list[str]:
    with tarfile.open(path, "r:gz") as tar:
        return tar.getnames()


def extract_archive(archive: Path, target: Path, arcname: str = "site") -> int:
    """Extract an archive made by :func:`create_archive` into ``target`` (which must not exist yet).

    Every member is checked first: absolute paths, ``..``, device files and links that point outside the target
    are refused, so a tampered archive cannot write anywhere else.
    """
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"{target} exists and is not empty")
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        links: set[tuple[str, ...]] = set()
        for member in members:
            rel = _check_member(member, target.resolve(), arcname)
            if rel is None:
                continue
            parts = PurePosixPath(rel).parts
            if any(parts[:i] in links for i in range(1, len(parts))):
                raise UnsafeArchiveError(f"member below a symlink: {member.name}")
            if member.issym():
                links.add(parts)
        for member in members:
            rel = _check_member(member, target.resolve(), arcname)
            if rel is None:
                continue
            member.name = rel
            if member.islnk():
                member.linkname = member.linkname[len(arcname) + 1 :]
            member.mode &= 0o7777 & ~0o6000  # never restore setuid/setgid bits
            if hasattr(tarfile, "data_filter"):
                tar.extract(member, target, set_attrs=True, filter="tar")
            else:  # pragma: no cover - Python without PEP 706 backport
                tar.extract(member, target, set_attrs=True)
            count += 1
    return count
