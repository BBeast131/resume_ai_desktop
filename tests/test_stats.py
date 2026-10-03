"""Dashboard counts for day / week / month, in the viewer's own time zone."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.stats import compute_dashboard, local_to_utc, period_for
from tests.factories import utc

NY = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")


@dataclass
class Row:
    status: str
    created_at: datetime
    applied_at: datetime | None = None
    shortlisted_at: datetime | None = None


def test_week_starts_on_monday_and_month_on_the_first():
    now = datetime(2026, 10, 7, 15, 30)  # a Wednesday
    week = period_for("week", now)
    assert (week.start, week.end) == (datetime(2026, 10, 5), datetime(2026, 10, 12))
    assert [bucket.label for bucket in week.buckets] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert (week.previous_start, week.previous_end) == (datetime(2026, 9, 28), datetime(2026, 10, 5))

    month = period_for("month", now)
    assert (month.start, month.end) == (datetime(2026, 10, 1), datetime(2026, 11, 1))
    assert len(month.buckets) == 31
    assert (month.previous_start, month.previous_end) == (datetime(2026, 9, 1), datetime(2026, 10, 1))

    day = period_for("day", now)
    assert (day.start, day.end) == (datetime(2026, 10, 7), datetime(2026, 10, 8))
    assert len(day.buckets) == 24


def test_generated_counts_whatever_the_status_is_now():
    now = utc(2026, 10, 7, 18)
    rows = [
        Row("generated", utc(2026, 10, 6, 12)),
        Row("applied", utc(2026, 10, 6, 13), applied_at=utc(2026, 10, 6, 15)),
        Row("shortlisted", utc(2026, 10, 6, 14), applied_at=utc(2026, 10, 6, 16), shortlisted_at=utc(2026, 10, 7, 9)),
        Row("rejected", utc(2026, 10, 5, 12), applied_at=utc(2026, 10, 5, 13)),
    ]
    data = compute_dashboard(rows, "week", now, NY)
    # All four were created this week: a later status never removes a resume from Generated.
    assert data.generated.value == 4
    # Shortlisted and rejected resumes were applied first, so they count as Applied.
    assert data.applied.value == 3
    assert data.shortlisted.value == 1
    assert data.breakdown == {"applied": 1, "shortlisted": 1, "rejected": 1, "generated": 1}
    assert data.breakdown_total == 4


def test_applied_needs_a_submitted_status():
    # Applied, then moved back to "generated": the history stays, the count does not.
    row = Row("generated", utc(2026, 10, 6, 12), applied_at=utc(2026, 10, 6, 15))
    data = compute_dashboard([row], "week", utc(2026, 10, 7, 18), NY)
    assert data.generated.value == 1 and data.applied.value == 0


def test_shortlisted_needs_the_current_status():
    row = Row("rejected", utc(2026, 10, 6, 12), applied_at=utc(2026, 10, 6, 13), shortlisted_at=utc(2026, 10, 6, 14))
    data = compute_dashboard([row], "week", utc(2026, 10, 7, 18), NY)
    assert data.shortlisted.value == 0 and data.applied.value == 1


def test_the_day_is_the_viewers_day_not_utc():
    # 2026-10-07 02:30 UTC is still Oct 6 (22:30) in New York, and already Oct 7 (11:30) in Tokyo.
    row = Row("generated", utc(2026, 10, 7, 2, 30))

    ny_evening = compute_dashboard([row], "day", utc(2026, 10, 7, 3), NY)  # Oct 6, 23:00 in New York
    assert ny_evening.generated.value == 1
    assert ny_evening.series_generated[22] == 1

    ny_next_day = compute_dashboard([row], "day", utc(2026, 10, 7, 15), NY)  # Oct 7, 11:00 in New York
    assert ny_next_day.generated.value == 0
    assert ny_next_day.generated.previous == 1

    tokyo = compute_dashboard([row], "day", utc(2026, 10, 7, 3), TOKYO)
    assert tokyo.generated.value == 1
    assert tokyo.series_generated[11] == 1


def test_week_and_month_boundaries_follow_the_time_zone():
    # Monday 00:30 in Tokyo is still Sunday in UTC and in New York.
    row = Row("generated", utc(2026, 10, 4, 15, 30))  # Mon Oct 5 00:30 JST / Sun Oct 4 11:30 EDT
    now = utc(2026, 10, 6, 12)
    assert compute_dashboard([row], "week", now, TOKYO).generated.value == 1
    assert compute_dashboard([row], "week", now, NY).generated.value == 0
    assert compute_dashboard([row], "week", now, NY).generated.previous == 1

    # The last evening of September in New York is already October in UTC.
    late = Row("generated", utc(2026, 10, 1, 2))  # Sep 30, 22:00 EDT
    assert compute_dashboard([late], "month", now, NY).generated.value == 0
    assert compute_dashboard([late], "month", now, TOKYO).generated.value == 1


def test_series_buckets_add_up_to_the_cards():
    now = utc(2026, 10, 9, 20)
    rows = [
        Row("applied", utc(2026, 10, 5, 14), applied_at=utc(2026, 10, 6, 14)),
        Row("applied", utc(2026, 10, 6, 14), applied_at=utc(2026, 10, 6, 15)),
        Row("shortlisted", utc(2026, 10, 7, 14), applied_at=utc(2026, 10, 8, 14), shortlisted_at=utc(2026, 10, 9, 14)),
    ]
    data = compute_dashboard(rows, "week", now, NY)
    assert data.series_generated == (1, 1, 1, 0, 0, 0, 0)
    assert data.series_applied == (0, 2, 0, 1, 0, 0, 0)
    assert data.series_shortlisted == (0, 0, 0, 0, 1, 0, 0)
    assert sum(data.series_generated) == data.generated.value
    assert sum(data.series_applied) == data.applied.value


def test_delta_against_the_previous_period():
    rows = [
        Row("generated", utc(2026, 10, 6, 14)),
        Row("generated", utc(2026, 9, 29, 14)),
        Row("generated", utc(2026, 9, 30, 14)),
    ]
    data = compute_dashboard(rows, "week", utc(2026, 10, 7, 18), NY)
    assert (data.generated.value, data.generated.previous, data.generated.delta) == (1, 2, -1)
    assert data.generated.delta_percent == -50.0
    assert data.applied.delta_percent is None  # nothing to compare with


def test_empty_state():
    data = compute_dashboard([], "month", utc(2026, 10, 7, 18), NY)
    assert data.empty and data.breakdown_total == 0


def test_period_bounds_convert_back_to_utc_for_drill_down():
    week = period_for("week", datetime(2026, 10, 7, 15))
    assert local_to_utc(week.start, NY) == utc(2026, 10, 5, 4)
    assert local_to_utc(week.start, TOKYO) == utc(2026, 10, 4, 15)
