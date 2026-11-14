from pathlib import Path

import pytest

from cloudpanel_backup.config import SAMPLE_CONFIG, ConfigError, load_config, parse_config, require_env


def test_sample_config_parses(tmp_path: Path):
    path = tmp_path / "c.toml"
    path.write_text(SAMPLE_CONFIG)
    cfg = load_config(path)
    assert cfg.storage.type == "s3"
    assert cfg.retention.daily == 7
    assert cfg.source == path


def test_defaults():
    cfg = parse_config({})
    assert cfg.storage.type == "local"
    assert cfg.storage.path is not None
    assert cfg.backup.wants_site("anything.example")
    assert "node_modules" in cfg.backup.all_excludes()
    assert cfg.backup.all_excludes(["x"])[-1] == "x"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"nope": {}}, "unknown section"),
        ({"backup": {"colour": 1}}, "unknown key"),
        ({"database": {"password": "x"}}, "environment variable"),
        ({"storage": {"type": "ftp"}}, "type must be"),
        ({"storage": {"type": "s3"}}, "bucket is required"),
        ({"storage": {"type": "local", "path": ""}}, "path is required"),
        ({"storage": {"type": "rsync"}}, "target is required"),
        ({"storage": {"type": "s3", "bucket": "b", "backend": "x"}}, "backend"),
        ({"encryption": {"method": "age"}}, "recipients"),
        ({"encryption": {"method": "gpg"}}, "gpg_recipient"),
        ({"encryption": {"method": "rot13"}}, "method must be"),
        ({"retention": {"daily": -1}}, "negative"),
        ({"retention": {"daily": "7"}}, "integer"),
        ({"backup": {"databases": "yes"}}, "true or false"),
        ({"backup": {"sites": [1]}}, "list of strings"),
        ({"backup": {"db_method": "x"}}, "db_method"),
        ({"backup": {"compression_level": 11}}, "compression_level"),
        ({"notify": {"on": "sometimes"}}, "notify"),
        ({"site": {"domain": "x"}}, "[[site]]"),
        ({"site": [{"skip": True}]}, "needs a domain"),
        ({"site": [{"domain": "a.example", "color": 1}]}, "unknown key"),
        ({"backup": 3}, "must be a table"),
    ],
)
def test_invalid_configs(data, message):
    with pytest.raises(ConfigError, match=message.replace("[", r"\[")):
        parse_config(data)


def test_site_overrides_and_patterns():
    cfg = parse_config(
        {
            "backup": {"sites": ["*.example.com"], "exclude_sites": ["staging.*"], "exclude": "tmp"},
            "site": [{"domain": "Shop.Example.com", "databases": ["a"], "exclude": ["var"]}],
        }
    )
    assert cfg.backup.exclude == ["tmp"]
    assert cfg.backup.wants_site("shop.example.com")
    assert not cfg.backup.wants_site("staging.example.com")
    assert not cfg.backup.wants_site("other.org")
    assert cfg.override_for("shop.example.com").databases == ["a"]


def test_missing_and_broken_files(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("[backup\n")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_require_env(monkeypatch):
    monkeypatch.delenv("CPB_TEST_X", raising=False)
    with pytest.raises(ConfigError, match="CPB_TEST_X"):
        require_env("CPB_TEST_X", "test")
    monkeypatch.setenv("CPB_TEST_X", "v")
    assert require_env("CPB_TEST_X", "test") == "v"
