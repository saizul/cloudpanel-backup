import io
import tarfile
from pathlib import Path

import pytest

from cloudpanel_backup.archive import (
    ExcludeMatcher,
    UnsafeArchiveError,
    create_archive,
    extract_archive,
    list_archive,
)
from cloudpanel_backup.config import DEFAULT_EXCLUDES
from conftest import make_site


@pytest.mark.parametrize(
    ("path", "excluded"),
    [
        ("node_modules", True),
        ("app/node_modules/x/index.js", True),
        ("wp-content/cache", True),
        ("wp-content/cache/page/index.html", True),
        ("sub/wp-content/cache/x", True),
        ("wp-content/uploads/cache-photo.jpg", False),
        ("debug.log", True),
        ("logs/app.txt", False),
        ("index.php", False),
    ],
)
def test_default_excludes(path, excluded):
    assert ExcludeMatcher(DEFAULT_EXCLUDES).matches(path) is excluded


def test_anchored_pattern_only_matches_at_root():
    matcher = ExcludeMatcher(["/private", "/var/tmp/"])
    assert matcher.matches("private/a")
    assert not matcher.matches("sub/private/a")
    assert matcher.matches("var/tmp")
    assert not ExcludeMatcher(["", "  "]).matches("anything")


def test_create_and_extract_roundtrip(tmp_path: Path):
    site = make_site(tmp_path / "home", "u", "example.com")
    (site / "link").symlink_to("index.php")
    out = tmp_path / "files.tar.gz"
    stats = create_archive(site, out, DEFAULT_EXCLUDES)
    names = list_archive(out)
    assert "site/index.php" in names
    assert "site/wp-content/uploads/2026/photo.jpg" in names
    assert not any("node_modules" in n or "/cache" in n or n.endswith(".log") for n in names)
    assert stats.files == 3  # index.php, photo.jpg, wp-config.php
    assert stats.symlinks == 1
    assert stats.excluded >= 3
    target = tmp_path / "restored"
    count = extract_archive(out, target)
    assert count >= 4
    assert (target / "index.php").read_text() == "<?php echo 'hello';\n"
    assert (target / "link").is_symlink()


def test_extract_refuses_non_empty_target(tmp_path: Path):
    site = make_site(tmp_path / "home", "u", "example.com")
    out = tmp_path / "a.tar.gz"
    create_archive(site, out)
    target = tmp_path / "t"
    target.mkdir()
    (target / "x").write_text("x")
    with pytest.raises(FileExistsError):
        extract_archive(out, target)


def _evil_archive(path: Path, members: list[tarfile.TarInfo]) -> None:
    with tarfile.open(path, "w:gz") as tar:
        root = tarfile.TarInfo("site")
        root.type = tarfile.DIRTYPE
        tar.addfile(root)
        for member in members:
            data = b"pwned" if member.isfile() else None
            if data is not None:
                member.size = len(data)
            tar.addfile(member, io.BytesIO(data) if data is not None else None)


def _member(name: str, kind: bytes = tarfile.REGTYPE, link: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = link
    return info


@pytest.mark.parametrize(
    "members",
    [
        [_member("site/../escape.txt")],
        [_member("other/file.txt")],
        [_member("site/link", tarfile.SYMTYPE, "../../etc")],
        [_member("site/dir", tarfile.SYMTYPE, "/etc"), _member("site/dir/passwd")],
        [_member("site/hard", tarfile.LNKTYPE, "/etc/passwd")],
        [_member("site/dev", tarfile.CHRTYPE)],
    ],
)
def test_extract_refuses_unsafe_members(tmp_path: Path, members):
    archive = tmp_path / "evil.tar.gz"
    _evil_archive(archive, members)
    with pytest.raises(UnsafeArchiveError):
        extract_archive(archive, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_setuid_bits_are_dropped(tmp_path: Path):
    archive = tmp_path / "suid.tar.gz"
    member = _member("site/tool")
    member.mode = 0o4755
    _evil_archive(archive, [member])
    extract_archive(archive, tmp_path / "out")
    assert (tmp_path / "out" / "tool").stat().st_mode & 0o7777 == 0o755
