from datetime import datetime, timedelta, timezone

from cloudpanel_backup.config import RetentionSettings
from cloudpanel_backup.retention import plan_retention, stale_incomplete


def daily_backups(days: int) -> list[datetime]:
    start = datetime(2026, 11, 14, 2, 30, tzinfo=timezone.utc)
    return [start - timedelta(days=i) for i in range(days)]


def test_gfs_keeps_daily_weekly_monthly():
    moments = daily_backups(400)
    plan = plan_retention(moments, RetentionSettings(daily=7, weekly=4, monthly=6))
    kept = sorted(plan.keep, reverse=True)
    # 7 daily; weekly and monthly overlap with the newest dailies
    assert all(m in plan.keep for m in moments[:7])
    weekly = [m for m in kept if "weekly" in plan.reasons(m)]
    monthly = [m for m in kept if "monthly" in plan.reasons(m)]
    assert len(weekly) == 4 and len(monthly) == 6
    assert len({(m.year, m.month) for m in monthly}) == 6
    assert len(plan.keep) + len(plan.delete) == 400
    assert len(plan.keep) < 20


def test_multiple_backups_per_day_keep_newest():
    base = datetime(2026, 11, 14, tzinfo=timezone.utc)
    moments = [base + timedelta(hours=h) for h in (1, 5, 9)]
    plan = plan_retention(moments, RetentionSettings(daily=1, weekly=0, monthly=0, min_keep=0))
    assert list(plan.keep) == [moments[-1]]
    assert sorted(plan.delete) == moments[:2]


def test_gaps_do_not_eat_retention():
    # a server that was off for a month still keeps 7 daily backups
    moments = daily_backups(3) + [m - timedelta(days=40) for m in daily_backups(10)]
    plan = plan_retention(moments, RetentionSettings(daily=7, weekly=0, monthly=0, min_keep=0))
    assert len(plan.keep) == 7


def test_min_keep_and_zero_policy():
    moments = daily_backups(5)
    plan = plan_retention(moments, RetentionSettings(daily=0, weekly=0, monthly=0, min_keep=2))
    assert sorted(plan.keep, reverse=True) == moments[:2]
    assert plan.reasons(moments[0]) == ["newest"]


def test_stale_incomplete():
    now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    recent = now - timedelta(hours=2)
    old = now - timedelta(days=2)
    assert stale_incomplete([recent, old], now) == [old]
