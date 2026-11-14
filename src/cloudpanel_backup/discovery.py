"""Find the sites of a CloudPanel server.

CloudPanel keeps every site in ``/home/<site-user>/htdocs/<domain>`` and writes one nginx vhost per site to
``/etc/nginx/sites-enabled/<domain>.conf``. ``clpctl`` has no command that lists sites (the documented list commands
are ``user:list`` and ``vhost-templates:list``), so discovery reads the vhosts first and then scans the htdocs
directories for anything the vhosts did not mention.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from cloudpanel_backup.appconfig import DbCredentials, detect_credentials
from cloudpanel_backup.config import Config
from cloudpanel_backup.nginx import Vhost, load_vhosts
from cloudpanel_backup.runner import CommandRunner

log = logging.getLogger(__name__)

# /home/clp is CloudPanel's own application; never treat it as a customer site.
SYSTEM_USERS = frozenset({"clp", "root", "lost+found"})
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$", re.I)


@dataclass
class Site:
    domain: str
    user: str
    site_dir: Path
    docroot: Path | None = None
    vhost_path: Path | None = None
    server_names: list[str] = field(default_factory=list)
    kind: str = "generic"
    databases: list[DbCredentials] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    source: str = "filesystem"

    @property
    def database_names(self) -> list[str]:
        return [db.name for db in self.databases]

    def to_dict(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "user": self.user,
            "site_dir": str(self.site_dir),
            "docroot": str(self.docroot) if self.docroot else None,
            "vhost": str(self.vhost_path) if self.vhost_path else None,
            "server_names": self.server_names,
            "kind": self.kind,
            "databases": self.database_names,
            "source": self.source,
        }


def looks_like_domain(value: str) -> bool:
    return bool(_DOMAIN_RE.match(value))


def split_site_path(root: str, home_root: Path) -> tuple[str, Path] | None:
    """``/home/john/htdocs/www.example.com/public`` -> ("john", Path("/home/john/htdocs/www.example.com"))."""
    try:
        rel = Path(root).relative_to(home_root)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 3 or parts[1] != "htdocs":
        return None
    return parts[0], home_root / parts[0] / "htdocs" / parts[2]


def _kind_from_vhost(vhost: Vhost | None) -> str:
    if vhost is None:
        return "generic"
    proxy = vhost.proxy_pass
    if proxy and not proxy.startswith(("http://127.0.0.1:8080", "http://localhost:8080")):
        return "proxy"
    if vhost.uses_php:
        return "php"
    return "static"


def _vhost_domain(vhost: Vhost) -> str | None:
    stem = vhost.path.name.removesuffix(".conf")
    if looks_like_domain(stem):
        return stem.lower()
    for name in vhost.server_names:
        if looks_like_domain(name):
            return name
    return None


def discover_sites(config: Config, runner: CommandRunner | None = None) -> list[Site]:
    settings = config.backup
    home_root = settings.home_root
    sites: dict[Path, Site] = {}

    for vhost in load_vhosts(settings.nginx_dir):
        root = vhost.root
        if root is None:
            continue
        split = split_site_path(root, home_root)
        if split is None:
            continue
        user, site_dir = split
        if user in SYSTEM_USERS:
            continue
        domain = _vhost_domain(vhost) or site_dir.name.lower()
        sites[site_dir] = Site(
            domain=domain,
            user=user,
            site_dir=site_dir,
            docroot=Path(root),
            vhost_path=vhost.path,
            server_names=vhost.server_names,
            kind=_kind_from_vhost(vhost),
            source="nginx",
        )

    if home_root.is_dir():
        for user_dir in sorted(home_root.iterdir()):
            if user_dir.name in SYSTEM_USERS or not (user_dir / "htdocs").is_dir():
                continue
            try:
                entries = sorted((user_dir / "htdocs").iterdir())
            except PermissionError:
                log.warning("cannot read %s (permission denied); run as root to include it", user_dir / "htdocs")
                continue
            for site_dir in entries:
                if not site_dir.is_dir() or site_dir.is_symlink() or site_dir in sites:
                    continue
                sites[site_dir] = Site(domain=site_dir.name.lower(), user=user_dir.name, site_dir=site_dir)

    result: list[Site] = []
    for site in sorted(sites.values(), key=lambda s: s.domain):
        override = config.override_for(site.domain)
        if override and override.skip:
            log.info("skipping %s (skip = true)", site.domain)
            continue
        if not settings.wants_site(site.domain):
            continue
        if override and override.path:
            site.site_dir = Path(override.path)
        if site.site_dir.is_dir():
            kind, creds = detect_credentials(site.site_dir, site.docroot)
            if kind != "generic" or site.kind == "generic":
                site.kind = kind if kind != "generic" else site.kind
            site.databases = creds
        if override:
            site.excludes = list(override.exclude)
            if override.databases is not None:
                known = {db.name: db for db in site.databases}
                site.databases = [
                    known.get(name, DbCredentials(name=name, source="config")) for name in override.databases
                ]
        if not settings.databases:
            site.databases = []
        result.append(site)
    return result


def is_cloudpanel(runner: CommandRunner) -> bool:
    return runner.which("clpctl") is not None


_CRED_KEYS = {
    "host": "host",
    "hostname": "host",
    "port": "port",
    "user": "user",
    "username": "user",
    "user name": "user",
    "password": "password",
}


def parse_master_credentials(output: str) -> DbCredentials | None:
    """Parse ``clpctl db:show:master-credentials`` output.

    The exact layout is not documented, so this accepts ``Key: value`` lines as well as the ``| Key | value |`` rows
    of a console table.
    """
    found: dict[str, str] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line or set(line) <= set("+-|= "):
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 2:
                key, value = cells[0], cells[1]
            else:
                continue
        elif ":" in line:
            key, _, value = line.partition(":")
        else:
            continue
        norm = _CRED_KEYS.get(key.strip().lower())
        if norm and value.strip():
            found[norm] = value.strip()
    if "user" not in found or "password" not in found:
        return None
    port = int(found["port"]) if found.get("port", "").isdigit() else 3306
    host = found.get("host", "127.0.0.1")
    return DbCredentials(name="", user=found["user"], password=found["password"], host=host, port=port, source="clpctl")


def master_credentials(runner: CommandRunner) -> DbCredentials | None:
    if not is_cloudpanel(runner):
        return None
    try:
        result = runner.run(["clpctl", "db:show:master-credentials"])
    except Exception as exc:
        log.warning("clpctl db:show:master-credentials failed: %s", exc)
        return None
    return parse_master_credentials(result.stdout)
