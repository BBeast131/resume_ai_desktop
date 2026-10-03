"""Dashboard numbers, computed from local rows.

Pure functions: they take the rows and "now" and return the cards, the chart
series and the status breakdown. The same code serves my own dashboard (rows
from SQLite) and the admin view of another user (rows from the web app).

Periods are in the viewer's local time:
  day   = today
  week  = this week, Monday to now
  month = this calendar month
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Literal, Protocol

PeriodKey = Literal["day", "week", "month"]
PERIOD_KEYS: tuple[PeriodKey, ...] = ("day", "week", "month")
PERIOD_LABELS: dict[str, str] = {"day": "Day", "week": "Week", "month": "Month"}
SUBMITTED = ("applied", "shortlisted", "rejected")
BREAKDOWN_ORDER = ("applied", "shortlisted", "rejected", "generated")


class ResumeFacts(Protocol):
    """What the dashboard needs to know about one resume."""

    status: str
    created_at: datetime
    applied_at: datetime | None
    shortlisted_at: datetime | None


@dataclass(frozen=True)
class Bucket:
    label: str
    start: datetime  # naive, in the viewer's local time
    end: datetime


@dataclass(frozen=True)
class Period:
    key: PeriodKey
    start: datetime  # naive local
    end: datetime
    previous_start: datetime
    previous_end: datetime
    buckets: tuple[Bucket, ...]
    title: str


@dataclass(frozen=True)
class Card:
    value: int
    previous: int

    @property
    def delta(self) -> int:
        return self.value - self.previous

    @property
    def delta_percent(self) -> float | None:
        """Change against the previous period; None when there is nothing to compare with."""
        if self.previous == 0:
            return None
        return (self.value - self.previous) / self.previous * 100.0


@dataclass(frozen=True)
class DashboardData:
    period: Period
    generated: Card
    applied: Card
    shortlisted: Card
    #: One value per bucket, for the bar chart.
    series_generated: tuple[int, ...]
    series_applied: tuple[int, ...]
    series_shortlisted: tuple[int, ...]
    #: Current status of the resumes generated in the period.
    breakdown: dict[str, int] = field(default_factory=dict)

    @property
    def breakdown_total(self) -> int:
        return sum(self.breakdown.values())

    @property
    def empty(self) -> bool:
        return self.generated.value == 0 and self.applied.value == 0 and self.shortlisted.value == 0


def to_local(value: datetime, tz: tzinfo | None) -> datetime:
    """A UTC instant as naive local wall-clock time. `tz=None` means this PC's time zone."""
    return value.astimezone(tz).replace(tzinfo=None)


def local_to_utc(value: datetime, tz: tzinfo | None) -> datetime:
    """The reverse of `to_local`, for turning a period into database bounds."""
    from datetime import UTC

    if tz is None:
        return value.astimezone().astimezone(UTC)
    return value.replace(tzinfo=tz).astimezone(UTC)


def period_for(key: PeriodKey, now_local: datetime) -> Period:
    """The period containing `now_local` (naive local time), with its buckets."""
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    if key == "day":
        start, end = midnight, midnight + timedelta(days=1)
        buckets = tuple(
            Bucket(f"{hour:02d}", start + timedelta(hours=hour), start + timedelta(hours=hour + 1))
            for hour in range(24)
        )
        return Period(
            key,
            start,
            end,
            start - timedelta(days=1),
            start,
            buckets,
            _day_title(start),
        )

    if key == "week":
        start = midnight - timedelta(days=midnight.weekday())  # Monday
        end = start + timedelta(days=7)
        buckets = tuple(
            Bucket(
                (start + timedelta(days=day)).strftime("%a"),
                start + timedelta(days=day),
                start + timedelta(days=day + 1),
            )
            for day in range(7)
        )
        title = f"{_short(start)} – {_short(end - timedelta(days=1))}"
        return Period(key, start, end, start - timedelta(days=7), start, buckets, title)

    start = midnight.replace(day=1)
    days = calendar.monthrange(start.year, start.month)[1]
    end = start + timedelta(days=days)
    previous_end = start
    previous_start = (start - timedelta(days=1)).replace(day=1)
    buckets = tuple(
        Bucket(str(day + 1), start + timedelta(days=day), start + timedelta(days=day + 1)) for day in range(days)
    )
    return Period("month", start, end, previous_start, previous_end, buckets, start.strftime("%B %Y"))


def _short(value: datetime) -> str:
    return f"{value.strftime('%b')} {value.day}"


def _day_title(value: datetime) -> str:
    return f"{value.strftime('%a')}, {value.strftime('%b')} {value.day}"


def _within(value: datetime | None, start: datetime, end: datetime, tz: tzinfo | None) -> bool:
    if value is None:
        return False
    local = to_local(value, tz)
    return start <= local < end


def counts_in(rows: Iterable[ResumeFacts], start: datetime, end: datetime, tz: tzinfo | None) -> tuple[int, int, int]:
    """(generated, applied, shortlisted) for one window.

    Generated: created in the window, whatever the status is now.
    Applied: `applied_at` in the window and the status is still a submitted one.
    Shortlisted: `shortlisted_at` in the window and the status is shortlisted.
    """
    generated = applied = shortlisted = 0
    for row in rows:
        if _within(row.created_at, start, end, tz):
            generated += 1
        if row.status in SUBMITTED and _within(row.applied_at, start, end, tz):
            applied += 1
        if row.status == "shortlisted" and _within(row.shortlisted_at, start, end, tz):
            shortlisted += 1
    return generated, applied, shortlisted


def compute_dashboard(
    rows: Iterable[ResumeFacts], key: PeriodKey, now: datetime, tz: tzinfo | None = None
) -> DashboardData:
    """`now` is an aware datetime; `tz` is the viewer's zone (None = this PC's)."""
    materialised = list(rows)
    period = period_for(key, to_local(now, tz))

    current = counts_in(materialised, period.start, period.end, tz)
    previous = counts_in(materialised, period.previous_start, period.previous_end, tz)

    series = [counts_in(materialised, bucket.start, bucket.end, tz) for bucket in period.buckets]

    breakdown = {status: 0 for status in BREAKDOWN_ORDER}
    for row in materialised:
        if _within(row.created_at, period.start, period.end, tz):
            breakdown[row.status if row.status in breakdown else "generated"] += 1

    return DashboardData(
        period=period,
        generated=Card(current[0], previous[0]),
        applied=Card(current[1], previous[1]),
        shortlisted=Card(current[2], previous[2]),
        series_generated=tuple(item[0] for item in series),
        series_applied=tuple(item[1] for item in series),
        series_shortlisted=tuple(item[2] for item in series),
        breakdown=breakdown,
    )
