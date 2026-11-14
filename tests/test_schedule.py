from pathlib import Path

import pytest

from cloudpanel_backup.schedule import ScheduleOptions, cron_entry, install_systemd, systemd_units


def test_systemd_units():
    units = systemd_units(ScheduleOptions(verify_after=True))
    service = units["cloudpanel-backup.service"]
    timer = units["cloudpanel-backup.timer"]
    assert "Type=oneshot" in service
    assert "ExecStart=/usr/local/bin/cloudpanel-backup --config /etc/cloudpanel-backup/config.toml" in service
    assert service.count("ExecStart=") == 2 and service.rstrip().endswith("TimeoutStartSec=6h")
    assert "EnvironmentFile=-/etc/cloudpanel-backup/env" in service
    assert "OnCalendar=*-*-* 02:30:00" in timer and "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


def test_cron_entry_quotes_paths():
    line = cron_entry(ScheduleOptions(config=Path("/etc/my backups/c.toml"), cron="15 3 * * 1-5"))
    assert "15 3 * * 1-5 root /usr/local/bin/cloudpanel-backup --config '/etc/my backups/c.toml'" in line
    assert line.strip().endswith(">> /var/log/cloudpanel-backup.log 2>&1")


@pytest.mark.parametrize(
    "opts",
    [
        ScheduleOptions(on_calendar="daily; rm -rf /"),
        ScheduleOptions(cron="* * *"),
        ScheduleOptions(cron="* * * * $(id)"),
        ScheduleOptions(name="Bad Name"),
    ],
)
def test_rejects_bad_values(opts):
    with pytest.raises(ValueError):
        systemd_units(opts) if opts.cron == "30 2 * * *" else cron_entry(opts)


def test_install(tmp_path: Path):
    written = install_systemd(ScheduleOptions(), tmp_path)
    assert sorted(p.name for p in written) == ["cloudpanel-backup.service", "cloudpanel-backup.timer"]
