"""Unreadable htdocs folders are skipped with a warning instead of stopping discovery."""

import os
import sys

import pytest

from cloudpanel_backup.discovery import discover_sites
from conftest import make_config


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions as a non-root user")
def test_unreadable_htdocs_is_skipped(server, caplog: pytest.LogCaptureFixture) -> None:
    locked = server["home"] / "stranger" / "htdocs"
    (locked / "hidden.example").mkdir(parents=True)
    locked.chmod(0o000)
    try:
        with caplog.at_level("WARNING"):
            domains = {s.domain for s in discover_sites(make_config(server))}
    finally:
        locked.chmod(0o755)
    assert "shop.example.com" in domains
    assert "hidden.example" not in domains
    assert "permission denied" in caplog.text


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions as a non-root user")
def test_unreadable_home_dir_is_skipped(server, caplog: pytest.LogCaptureFixture) -> None:
    home = server["home"] / "locked-user"
    (home / "htdocs" / "x.example").mkdir(parents=True)
    home.chmod(0o000)
    try:
        with caplog.at_level("WARNING"):
            domains = {s.domain for s in discover_sites(make_config(server))}
    finally:
        home.chmod(0o755)
    assert "shop.example.com" in domains
    assert "x.example" not in domains
