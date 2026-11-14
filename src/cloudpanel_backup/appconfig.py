"""Read database credentials from application config files (WordPress, Laravel and other .env based apps)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# define( 'DB_NAME', 'value' );  define("DB_HOST", "localhost:3306");
_DEFINE_RE = re.compile(
    r"""define\s*\(\s*(['"])(?P<key>DB_[A-Z_]+)\1\s*,\s*(?P<q>['"])(?P<value>(?:\\.|(?!(?P=q)).)*)(?P=q)\s*\)""",
    re.S,
)
_PREFIX_RE = re.compile(r"""\$table_prefix\s*=\s*(['"])(?P<value>[^'"]*)\1\s*;""")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT_RE = re.compile(r"^\s*(//|#).*$", re.M)


@dataclass
class DbCredentials:
    name: str
    user: str | None = None
    password: str | None = None
    host: str = "127.0.0.1"
    port: int = 3306
    socket: str | None = None
    source: str = ""
    table_prefix: str | None = None

    def __repr__(self) -> str:  # never print the password
        return (
            f"DbCredentials(name={self.name!r}, user={self.user!r}, host={self.host!r}, port={self.port}, "
            f"source={self.source!r})"
        )


def _php_unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def split_host(raw: str) -> tuple[str, int, str | None]:
    """Split a WordPress DB_HOST value: ``localhost``, ``127.0.0.1:3307``, ``localhost:/run/mysqld/mysqld.sock``."""
    host, port, sock = raw.strip() or "127.0.0.1", 3306, None
    if host.startswith("[") and "]" in host:  # [::1]:3306
        addr, _, rest = host[1:].partition("]")
        host = addr
        if rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
        return host, port, None
    if ":" in host and host.count(":") == 1:
        name, _, tail = host.partition(":")
        if tail.isdigit():
            host, port = name, int(tail)
        elif tail.startswith("/"):
            host, sock = name, tail
    if host == "localhost":
        host = "127.0.0.1" if sock is None else host
    return host, port, sock


def parse_wp_config(text: str) -> DbCredentials | None:
    stripped = _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", text))
    values = {m.group("key"): _php_unescape(m.group("value")) for m in _DEFINE_RE.finditer(stripped)}
    name = values.get("DB_NAME")
    if not name:
        return None
    host, port, sock = split_host(values.get("DB_HOST", "localhost"))
    prefix_match = _PREFIX_RE.search(stripped)
    return DbCredentials(
        name=name,
        user=values.get("DB_USER"),
        password=values.get("DB_PASSWORD"),
        host=host,
        port=port,
        socket=sock,
        source="wp-config.php",
        table_prefix=prefix_match.group("value") if prefix_match else None,
    )


def parse_dotenv(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key.strip()] = value
    return values


def parse_laravel_env(text: str) -> DbCredentials | None:
    env = parse_dotenv(text)
    if env.get("DB_CONNECTION", "mysql") not in ("mysql", "mariadb"):
        return None
    name = env.get("DB_DATABASE")
    if not name:
        return None
    host, port, sock = split_host(env.get("DB_HOST", "127.0.0.1"))
    if env.get("DB_PORT", "").isdigit():
        port = int(env["DB_PORT"])
    return DbCredentials(
        name=name,
        user=env.get("DB_USERNAME"),
        password=env.get("DB_PASSWORD"),
        host=host,
        port=port,
        socket=env.get("DB_SOCKET") or sock,
        source=".env",
    )


def find_wp_config(site_dir: Path, docroot: Path | None = None) -> Path | None:
    """WordPress looks for wp-config.php in its directory and one level above it."""
    candidates = []
    for base in [docroot, site_dir]:
        if base is None:
            continue
        candidates += [base / "wp-config.php", base.parent / "wp-config.php"]
    for path in candidates:
        if path.is_file():
            return path
    return None


def detect_credentials(site_dir: Path, docroot: Path | None = None) -> tuple[str, list[DbCredentials]]:
    """Return the application type and any database credentials found in the site's config files."""
    wp = find_wp_config(site_dir, docroot)
    if wp is not None:
        creds = parse_wp_config(wp.read_text(encoding="utf-8", errors="replace"))
        return "wordpress", [creds] if creds else []
    env_path = site_dir / ".env"
    if env_path.is_file():
        creds = parse_laravel_env(env_path.read_text(encoding="utf-8", errors="replace"))
        kind = "laravel" if (site_dir / "artisan").is_file() else "dotenv"
        return kind, [creds] if creds else []
    return "generic", []
