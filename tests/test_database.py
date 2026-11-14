import gzip
from pathlib import Path

import pytest

from cloudpanel_backup.appconfig import DbCredentials
from cloudpanel_backup.config import DatabaseSettings
from cloudpanel_backup.database import (
    client_options,
    dump_database,
    dump_program,
    import_database,
    resolve_credentials,
)
from conftest import FakeRunner


def test_dump_uses_single_transaction_and_option_file(tmp_path: Path):
    runner = FakeRunner()
    creds = DbCredentials(name="shop", user="u", password='pa"ss\\word')
    out = tmp_path / "db-shop.sql.gz"
    dump_database(runner, creds, out, DatabaseSettings())
    args, opts = runner.calls[0]
    assert args[0] == "mariadb-dump"
    assert args[1].startswith("--defaults-extra-file=")
    assert "--single-transaction" in args and "--quick" in args and "--routines" in args
    assert args[-1] == "shop"
    assert not any("pa" in a and "ss" in a for a in args), "password must not be on the command line"
    assert opts.gzip_stdout == 6
    assert gzip.decompress(Path(out).read_bytes()).startswith(b"-- dump of shop")
    assert 'password="pa\\"ss\\\\word"' in runner.cnf_contents[0]
    assert not Path(args[1].split("=", 1)[1]).exists(), "option file is removed afterwards"


def test_dump_output_name_is_checked(tmp_path: Path):
    with pytest.raises(ValueError):
        dump_database(FakeRunner(), DbCredentials(name="x"), tmp_path / "x.sql", DatabaseSettings())


def test_dump_with_clpctl(tmp_path: Path):
    runner = FakeRunner()
    out = tmp_path / "db-x.sql.gz"
    dump_database(runner, DbCredentials(name="x"), out, DatabaseSettings(), method="clpctl")
    assert runner.calls[0][0] == ["clpctl", "db:export", "--databaseName=x", f"--file={out}"]
    assert gzip.decompress(Path(out).read_bytes()).startswith(b"-- clpctl export of x")


def test_mysqldump_fallback():
    assert dump_program(FakeRunner(programs=["mysqldump"]), DatabaseSettings()) == "mysqldump"
    assert dump_program(FakeRunner(), DatabaseSettings()) == "mariadb-dump"


def test_import_streams_gunzipped_dump(tmp_path: Path):
    dump = tmp_path / "db.sql.gz"
    with gzip.open(dump, "wb") as fh:
        fh.write(b"SELECT 1;")
    runner = FakeRunner()
    import_database(runner, DbCredentials(name="target", user="u", password="p"), dump, DatabaseSettings())
    assert runner.imports["target"] == b"SELECT 1;"
    args, opts = runner.calls[0]
    assert args[0] == "mariadb" and opts.gunzip_stdin


def test_import_with_clpctl_runs_in_dump_directory(tmp_path: Path):
    dump = tmp_path / "db.sql.gz"
    with gzip.open(dump, "wb") as fh:
        fh.write(b"SELECT 2;")
    runner = FakeRunner()
    import_database(runner, DbCredentials(name="t"), dump, DatabaseSettings(), method="clpctl")
    args, opts = runner.calls[0]
    assert args == ["clpctl", "db:import", "--databaseName=t", "--file=db.sql.gz"]
    assert opts.cwd == tmp_path
    assert runner.imports["t"] == b"SELECT 2;"


def test_client_options_socket():
    text = client_options(DbCredentials(name="x", user="u", socket="/run/mysqld/mysqld.sock"))
    assert 'socket="/run/mysqld/mysqld.sock"' in text and "host=" not in text


def test_resolve_credentials_order(monkeypatch):
    site = DbCredentials(name="db", user="site", password="s")
    master = DbCredentials(name="", user="root", password="m")
    assert resolve_credentials(site, DatabaseSettings(), master).user == "site"
    assert resolve_credentials(DbCredentials(name="db"), DatabaseSettings(), master).user == "root"
    monkeypatch.setenv("CPB_DB_PASSWORD", "cfg")
    explicit = resolve_credentials(site, DatabaseSettings(user="backup", password_env="CPB_DB_PASSWORD"), master)
    assert (explicit.user, explicit.password, explicit.name) == ("backup", "cfg", "db")
    none = resolve_credentials(DbCredentials(name="db"), DatabaseSettings(use_master_credentials=False), master)
    assert none.user is None
