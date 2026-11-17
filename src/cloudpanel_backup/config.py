"""Configuration file (TOML) loading and validation.

Secrets are never stored in the file itself: every secret is referenced by the name of an environment variable
(``*_env`` keys), so the config can be committed or shared without leaking credentials.
"""

from __future__ import annotations

import fnmatch
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.10 only
    import tomli as tomllib

DEFAULT_CONFIG_PATH = Path("/etc/cloudpanel-backup/config.toml")

# Relative to the site directory /home/<user>/htdocs/<domain>. Matched against every path component sequence.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    "node_modules",
    ".git",
    "wp-content/cache",
    "wp-content/upgrade",
    "wp-content/backups",
    "wp-content/ai1wm-backups",
    "wp-content/updraft",
    "wp-content/uploads/cache",
    "wp-content/litespeed",
    "wp-content/et-cache",
    "storage/framework/cache",
    "storage/framework/sessions",
    "storage/framework/views",
    "var/cache",
    "var/session",
    "*.log",
    ".DS_Store",
)


class ConfigError(ValueError):
    """The configuration file is missing, unreadable or invalid."""


@dataclass
class SiteOverride:
    domain: str
    skip: bool = False
    databases: list[str] | None = None
    exclude: list[str] = field(default_factory=list)
    path: str | None = None


@dataclass
class BackupSettings:
    staging_dir: Path = Path("/var/backups/cloudpanel-backup")
    prefix: str = "cloudpanel"
    host: str | None = None
    sites: list[str] = field(default_factory=lambda: ["*"])
    exclude_sites: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    use_default_excludes: bool = True
    databases: bool = True
    db_method: str = "mariadb-dump"
    compression_level: int = 6
    keep_staging: bool = False
    home_root: Path = Path("/home")
    nginx_dir: Path = Path("/etc/nginx/sites-enabled")
    # Run lock shared by backup, restore and prune; defaults to <staging_dir>/cloudpanel-backup.lock.
    lock_file: Path | None = None
    lock_timeout: int = 0  # seconds to wait for another run to finish; 0 = give up at once

    def lock_path(self) -> Path:
        return self.lock_file or self.staging_dir / "cloudpanel-backup.lock"

    def all_excludes(self, extra: list[str] | None = None) -> list[str]:
        base = list(DEFAULT_EXCLUDES) if self.use_default_excludes else []
        return base + list(self.exclude) + list(extra or [])

    def wants_site(self, domain: str) -> bool:
        if any(fnmatch.fnmatch(domain, pattern) for pattern in self.exclude_sites):
            return False
        return any(fnmatch.fnmatch(domain, pattern) for pattern in self.sites)


@dataclass
class DatabaseSettings:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str | None = None
    password_env: str | None = None
    dump_program: str = "mariadb-dump"
    client_program: str = "mariadb"
    extra_dump_options: list[str] = field(default_factory=list)
    use_master_credentials: bool = True

    def password(self) -> str | None:
        return _env(self.password_env) if self.password_env else None


@dataclass
class StorageSettings:
    type: str = "local"
    # s3
    bucket: str | None = None
    endpoint: str | None = None
    region: str = "auto"
    access_key_env: str = "CPB_S3_ACCESS_KEY_ID"
    secret_key_env: str = "CPB_S3_SECRET_ACCESS_KEY"
    path_style: bool = True
    backend: str = "builtin"
    multipart_threshold_mb: int = 64
    storage_class: str | None = None
    # local
    path: Path | None = Path("/var/backups/cloudpanel-backup-archive")
    # rsync
    target: str | None = None
    ssh_port: int | None = None
    ssh_key: Path | None = None
    rsync_options: list[str] = field(default_factory=list)


@dataclass
class EncryptionSettings:
    method: str = "none"
    recipients: list[str] = field(default_factory=list)
    recipients_file: Path | None = None
    identity_file: Path | None = None
    gpg_recipient: str | None = None
    gpg_homedir: Path | None = None
    gpg_passphrase_env: str | None = None


@dataclass
class RetentionSettings:
    daily: int = 7
    weekly: int = 4
    monthly: int = 6
    min_keep: int = 1


@dataclass
class NotifySettings:
    on: str = "failure"
    telegram_bot_token_env: str | None = None
    telegram_chat_id: str | None = None
    webhook_url_env: str | None = None
    webhook_url: str | None = None
    email_to: list[str] = field(default_factory=list)
    email_from: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password_env: str | None = None
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    # Name of the environment variable that holds a Healthchecks.io-style ping URL.
    heartbeat_url_env: str | None = None


@dataclass
class Config:
    backup: BackupSettings = field(default_factory=BackupSettings)
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    encryption: EncryptionSettings = field(default_factory=EncryptionSettings)
    retention: RetentionSettings = field(default_factory=RetentionSettings)
    notify: NotifySettings = field(default_factory=NotifySettings)
    sites: dict[str, SiteOverride] = field(default_factory=dict)
    source: Path | None = None

    def override_for(self, domain: str) -> SiteOverride | None:
        return self.sites.get(domain.lower())


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def require_env(name: str, purpose: str) -> str:
    value = _env(name)
    if value is None:
        raise ConfigError(f"environment variable {name} is not set ({purpose})")
    return value


_SECTION_TYPES: dict[str, type] = {
    "backup": BackupSettings,
    "database": DatabaseSettings,
    "storage": StorageSettings,
    "encryption": EncryptionSettings,
    "retention": RetentionSettings,
    "notify": NotifySettings,
}

_PATH_FIELDS = {
    "staging_dir",
    "home_root",
    "nginx_dir",
    "path",
    "ssh_key",
    "recipients_file",
    "identity_file",
    "gpg_homedir",
    "lock_file",
}
_LIST_FIELDS = {
    "sites",
    "exclude_sites",
    "exclude",
    "extra_dump_options",
    "rsync_options",
    "recipients",
    "email_to",
}
_INT_FIELDS = {
    "compression_level",
    "port",
    "multipart_threshold_mb",
    "ssh_port",
    "daily",
    "weekly",
    "monthly",
    "min_keep",
    "smtp_port",
    "lock_timeout",
}
_BOOL_FIELDS = {
    "use_default_excludes",
    "databases",
    "keep_staging",
    "path_style",
    "use_master_credentials",
    "smtp_starttls",
    "smtp_ssl",
}
_SECRET_LOOKING = ("password", "secret", "token", "access_key", "passphrase")


def _build_section(name: str, raw: dict[str, Any]) -> Any:
    cls = _SECTION_TYPES[name]
    known = set(cls.__dataclass_fields__)
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        if any(word in key for word in _SECRET_LOOKING) and not key.endswith("_env"):
            raise ConfigError(f"[{name}] {key!r}: put secrets in an environment variable and use {key}_env")
        if key not in known:
            raise ConfigError(f"[{name}] unknown key {key!r}")
        if key in _PATH_FIELDS and value is not None:
            value = Path(str(value)).expanduser() if str(value) else None
        elif key in _LIST_FIELDS:
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(f"[{name}] {key!r} must be a list of strings")
        elif key in _INT_FIELDS:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"[{name}] {key!r} must be an integer")
        elif key in _BOOL_FIELDS and not isinstance(value, bool):
            raise ConfigError(f"[{name}] {key!r} must be true or false")
        kwargs[key] = value
    return cls(**kwargs)


def _validate(cfg: Config) -> None:
    b = cfg.backup
    if b.db_method not in ("mariadb-dump", "clpctl"):
        raise ConfigError("[backup] db_method must be 'mariadb-dump' or 'clpctl'")
    if not 0 <= b.compression_level <= 9:
        raise ConfigError("[backup] compression_level must be between 0 and 9")
    if b.lock_timeout < 0:
        raise ConfigError("[backup] lock_timeout cannot be negative")
    s = cfg.storage
    if s.type not in ("s3", "local", "rsync"):
        raise ConfigError("[storage] type must be 's3', 'local' or 'rsync'")
    if s.type == "s3":
        if not s.bucket:
            raise ConfigError("[storage] bucket is required for type = 's3'")
        if s.backend not in ("builtin", "boto3"):
            raise ConfigError("[storage] backend must be 'builtin' or 'boto3'")
    if s.type == "local" and not s.path:
        raise ConfigError("[storage] path is required for type = 'local'")
    if s.type == "rsync" and not s.target:
        raise ConfigError("[storage] target is required for type = 'rsync'")
    e = cfg.encryption
    if e.method not in ("none", "age", "gpg"):
        raise ConfigError("[encryption] method must be 'none', 'age' or 'gpg'")
    if e.method == "age" and not (e.recipients or e.recipients_file):
        raise ConfigError("[encryption] age needs recipients or recipients_file")
    if e.method == "gpg" and not e.gpg_recipient:
        raise ConfigError("[encryption] gpg needs gpg_recipient")
    r = cfg.retention
    if min(r.daily, r.weekly, r.monthly, r.min_keep) < 0:
        raise ConfigError("[retention] values cannot be negative")
    if cfg.notify.on not in ("always", "failure", "never"):
        raise ConfigError("[notify] on must be 'always', 'failure' or 'never'")


def parse_config(data: dict[str, Any], source: Path | None = None) -> Config:
    cfg = Config(source=source)
    for key, value in data.items():
        if key in _SECTION_TYPES:
            if not isinstance(value, dict):
                raise ConfigError(f"[{key}] must be a table")
            setattr(cfg, key, _build_section(key, value))
        elif key == "site":
            if not isinstance(value, list):
                raise ConfigError("use [[site]] tables for per-site overrides")
            for entry in value:
                domain = str(entry.get("domain", "")).lower()
                if not domain:
                    raise ConfigError("[[site]] needs a domain")
                unknown = set(entry) - set(SiteOverride.__dataclass_fields__)
                if unknown:
                    raise ConfigError(f"[[site]] {domain}: unknown key(s) {', '.join(sorted(unknown))}")
                cfg.sites[domain] = SiteOverride(
                    domain=domain,
                    skip=bool(entry.get("skip", False)),
                    databases=list(entry["databases"]) if "databases" in entry else None,
                    exclude=list(entry.get("exclude", [])),
                    path=entry.get("path"),
                )
        else:
            raise ConfigError(f"unknown section [{key}]")
    _validate(cfg)
    return cfg


def load_config(path: Path | None) -> Config:
    target = path or DEFAULT_CONFIG_PATH
    try:
        raw = target.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {target} (create one with 'cloudpanel-backup init-config')") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{target}: {exc}") from exc
    return parse_config(data, source=target)


SAMPLE_CONFIG = """\
# cloudpanel-backup configuration. Secrets are read from environment variables (*_env keys);
# keep them in /etc/cloudpanel-backup/env (mode 0600), which the systemd unit loads.

[backup]
staging_dir = "/var/backups/cloudpanel-backup"
prefix = "cloudpanel"
# host = "vps1"                 # defaults to the short hostname
sites = ["*"]                    # shell patterns of domains to back up
exclude_sites = []
exclude = []                     # extra excludes, added to the built-in list (cache, node_modules, ...)
databases = true
db_method = "mariadb-dump"       # or "clpctl" (clpctl db:export)
compression_level = 6
# lock_timeout = 0               # seconds to wait if another run holds the lock (0 = skip this run)

[database]
host = "127.0.0.1"
port = 3306
# user = "backup"                # optional: a user that can dump every database
# password_env = "CPB_DB_PASSWORD"
use_master_credentials = true    # fall back to 'clpctl db:show:master-credentials'

[storage]
type = "s3"                      # "s3", "local" or "rsync"
bucket = "my-backups"
endpoint = "https://<account-id>.r2.cloudflarestorage.com"
region = "auto"
access_key_env = "CPB_S3_ACCESS_KEY_ID"
secret_key_env = "CPB_S3_SECRET_ACCESS_KEY"
# backend = "boto3"              # needs: pip install "cloudpanel-backup[s3]"

[encryption]
method = "none"                  # "age" or "gpg"
# recipients = ["age1..."]
# identity_file = "/root/.config/cloudpanel-backup/age-identity.txt"   # only needed for restore

[retention]
daily = 7
weekly = 4
monthly = 6

[notify]
on = "failure"                   # "always", "failure" or "never"
# telegram_bot_token_env = "CPB_TELEGRAM_TOKEN"
# telegram_chat_id = "123456789"
# webhook_url_env = "CPB_WEBHOOK_URL"
# heartbeat_url_env = "CPB_HEARTBEAT_URL"   # Healthchecks.io / Uptime Kuma ping URL (start, success, fail)

# [[site]]
# domain = "shop.example.com"
# databases = ["shop"]
# exclude = ["var/import"]
"""
