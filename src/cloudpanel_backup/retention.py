"""Grandfather-father-son retention: keep the newest backup of each of the last N days, weeks and months."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from cloudpanel_backup.config import RetentionSettings


@dataclass
class RetentionPlan:
    keep: dict[datetime, list[str]] = field(default_factory=dict)
    delete: list[datetime] = field(default_factory=list)

    def reasons(self, moment: datetime) -> list[str]:
        return self.keep.get(moment, [])


def _bucket_keep(moments: list[datetime], count: int, key, label: str, plan: RetentionPlan) -> None:
    if count <= 0:
        return
    seen: set[object] = set()
    for moment in moments:  # newest first
        bucket = key(moment)
        if bucket in seen:
            continue
        seen.add(bucket)
        plan.keep.setdefault(moment, []).append(label)
        if len(seen) >= count:
            break


def plan_retention(moments: Iterable[datetime], settings: RetentionSettings) -> RetentionPlan:
    """Decide which complete backups to keep.

    Each rule keeps the newest backup in each of its last N periods that *have* a backup, so a server that was off
    for a week still keeps N daily backups. ``min_keep`` newest backups are always kept.
    """
    ordered = sorted(set(moments), reverse=True)
    plan = RetentionPlan()
    for moment in ordered[: settings.min_keep]:
        plan.keep.setdefault(moment, []).append("newest")
    _bucket_keep(ordered, settings.daily, lambda m: m.date(), "daily", plan)
    _bucket_keep(ordered, settings.weekly, lambda m: m.isocalendar()[:2], "weekly", plan)
    _bucket_keep(ordered, settings.monthly, lambda m: (m.year, m.month), "monthly", plan)
    plan.delete = [m for m in ordered if m not in plan.keep]
    return plan


def stale_incomplete(
    moments: Iterable[datetime], now: datetime, grace: timedelta = timedelta(hours=24)
) -> list[datetime]:
    """Incomplete backups (no manifest) older than ``grace`` are leftovers of failed runs."""
    return sorted(m for m in moments if now - m > grace)
