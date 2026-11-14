from pathlib import Path

import pytest

from cloudpanel_backup.appconfig import (
    detect_credentials,
    parse_dotenv,
    parse_laravel_env,
    parse_wp_config,
    split_host,
)
from conftest import WP_CONFIG


def test_wp_config_values_and_comments():
    creds = parse_wp_config(WP_CONFIG)
    assert creds is not None
    assert creds.name == "shop_wp"
    assert creds.user == "shop_user"
    assert creds.password == "p@ss'word\"x"
    assert creds.host == "127.0.0.1"
    assert creds.table_prefix == "wpx_"
    assert "p@ss" not in repr(creds)


def test_wp_config_double_quotes_and_block_comments():
    text = """<?php /* define('DB_NAME', 'old'); */
    define("DB_NAME", "new_db"); define("DB_USER","u"); define("DB_PASSWORD",""); define("DB_HOST","db.internal:3307");
    """
    creds = parse_wp_config(text)
    assert creds is not None
    assert (creds.name, creds.host, creds.port, creds.password) == ("new_db", "db.internal", 3307, "")


def test_wp_config_without_db_name():
    assert parse_wp_config("<?php echo 1;") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("localhost", ("127.0.0.1", 3306, None)),
        ("127.0.0.1:3307", ("127.0.0.1", 3307, None)),
        ("localhost:/run/mysqld/mysqld.sock", ("localhost", 3306, "/run/mysqld/mysqld.sock")),
        ("[::1]:3308", ("::1", 3308, None)),
        ("", ("127.0.0.1", 3306, None)),
    ],
)
def test_split_host(raw, expected):
    assert split_host(raw) == expected


def test_dotenv_parsing():
    env = parse_dotenv("A=1\n# c\nexport B=\"two words\"\nC=x # trailing\nD='q'\nbad line\n")
    assert env == {"A": "1", "B": "two words", "C": "x", "D": "q"}


def test_laravel_env():
    creds = parse_laravel_env(
        "DB_CONNECTION=mysql\nDB_HOST=127.0.0.1\nDB_PORT=3310\nDB_DATABASE=app\nDB_USERNAME=app\nDB_PASSWORD=secret\n"
    )
    assert creds is not None
    assert (creds.name, creds.port, creds.user, creds.source) == ("app", 3310, "app", ".env")
    assert parse_laravel_env("DB_CONNECTION=pgsql\nDB_DATABASE=x\n") is None
    assert parse_laravel_env("APP_NAME=x\n") is None


def test_detect_wordpress_above_docroot(tmp_path: Path):
    site = tmp_path / "site"
    (site / "public").mkdir(parents=True)
    (site / "wp-config.php").write_text(WP_CONFIG)
    kind, creds = detect_credentials(site, site / "public")
    assert kind == "wordpress"
    assert creds[0].name == "shop_wp"


def test_detect_laravel_and_generic(tmp_path: Path):
    (tmp_path / "artisan").write_text("#!/usr/bin/env php")
    (tmp_path / ".env").write_text("DB_DATABASE=lara\nDB_USERNAME=u\n")
    assert detect_credentials(tmp_path)[0] == "laravel"
    other = tmp_path / "other"
    other.mkdir()
    assert detect_credentials(other) == ("generic", [])
