"""systemd timer and cron entries for scheduled backups."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

_CALENDAR_SAFE = re.compile(r"^[A-Za-z0-9*:,./ ~-]+$")
_CRON_FIELD = re.compile(r"^[0-9*/,-]+$")


@dataclass
class ScheduleOptions:
    executable: str = "/usr/local/bin/cloudpanel-backup"
    config: Path = Path("/etc/cloudpanel-backup/config.toml")
    env_file: Path = Path("/etc/cloudpanel-backup/env")
    on_calendar: str = "*-*-* 02:30:00"
    randomized_delay: str = "15min"
    cron: str = "30 2 * * *"
    name: str = "cloudpanel-backup"
    verify_after: bool = False
    log_file: Path = Path("/var/log/cloudpanel-backup.log")


def _check(opts: ScheduleOptions) -> None:
    if not _CALENDAR_SAFE.match(opts.on_calendar):
        raise ValueError(f"unexpected characters in OnCalendar value {opts.on_calendar!r}")
    fields = opts.cron.split()
    if len(fields) != 5 or not all(_CRON_FIELD.match(f) for f in fields):
        raise ValueError(f"cron schedule must have five numeric fields, got {opts.cron!r}")
    if not re.match(r"^[a-z0-9-]+$", opts.name):
        raise ValueError("unit name may only contain a-z, 0-9 and '-'")


def _command(opts: ScheduleOptions, action: str) -> str:
    return shlex.join([opts.executable, "--config", str(opts.config), "--env-file", str(opts.env_file), action])


def systemd_units(opts: ScheduleOptions) -> dict[str, str]:
    """Return {file name: content} for a oneshot service and its timer."""
    _check(opts)
    exec_lines = [f"ExecStart={_command(opts, 'backup')}"]
    if opts.verify_after:
        exec_lines.append(f"ExecStart={_command(opts, 'verify')}")
    service = "\n".join(
        [
            "# Created by cloudpanel-backup schedule",
            "[Unit]",
            "Description=Back up CloudPanel sites",
            "Wants=network-online.target",
            "After=network-online.target mariadb.service mysql.service",
            "",
            "[Service]",
            "Type=oneshot",
            # the env file is optional; cloudpanel-backup --env-file reads it too, this keeps `systemctl show` honest
            f"EnvironmentFile=-{opts.env_file}",
            *exec_lines,
            "Nice=10",
            "IOSchedulingClass=idle",
            "PrivateTmp=true",
            "NoNewPrivileges=true",
            "ProtectKernelTunables=true",
            "ProtectControlGroups=true",
            "UMask=0077",
            "TimeoutStartSec=6h",
            "",
        ]
    )
    timer = "\n".join(
        [
            "# Created by cloudpanel-backup schedule",
            "[Unit]",
            "Description=Scheduled CloudPanel site backups",
            "",
            "[Timer]",
            f"OnCalendar={opts.on_calendar}",
            f"RandomizedDelaySec={opts.randomized_delay}",
            "Persistent=true",
            f"Unit={opts.name}.service",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )
    return {f"{opts.name}.service": service, f"{opts.name}.timer": timer}


def cron_entry(opts: ScheduleOptions) -> str:
    """A line for /etc/cron.d/ (with the user field)."""
    _check(opts)
    command = _command(opts, "backup")
    if opts.verify_after:
        command += " && " + _command(opts, "verify")
    log = shlex.quote(str(opts.log_file))
    return "\n".join(
        [
            "# Created by cloudpanel-backup schedule",
            "SHELL=/bin/sh",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            f"{opts.cron} root {command} >> {log} 2>&1",
            "",
        ]
    )


def install_systemd(opts: ScheduleOptions, directory: Path = Path("/etc/systemd/system")) -> list[Path]:
    written = []
    for name, content in systemd_units(opts).items():
        path = directory / name
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written
