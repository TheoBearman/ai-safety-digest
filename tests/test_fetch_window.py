"""Tests for the pipeline date window in scripts.fetch."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.fetch import _is_within_window
from scripts.models import Paper

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
CUTOFF = (NOW - timedelta(days=7)).replace(hour=0, minute=0, second=0)


def _paper(published_date: str) -> Paper:
    return Paper(
        title="A Test Paper With A Reasonable Title",
        authors=[],
        organization="Org",
        abstract="",
        url="https://example.com/paper",
        published_date=published_date,
        source_type="scrape",
        source_url="https://example.com",
    )


def test_recent_paper_is_kept():
    assert _is_within_window(_paper("2026-07-19T10:00:00+00:00"), CUTOFF, NOW)


def test_old_paper_is_dropped():
    assert not _is_within_window(_paper("2026-07-01T10:00:00+00:00"), CUTOFF, NOW)


def test_far_future_paper_is_dropped():
    """Listing cards sometimes carry scheduled/project dates (e.g. RAND
    project pages dated weeks ahead); those must not enter the digest."""
    assert not _is_within_window(_paper("2026-09-04T00:00:00+00:00"), CUTOFF, NOW)


def test_slightly_future_paper_survives_timezone_skew():
    """A same-day post stamped in a UTC+13 timezone can look up to ~13h
    'future' when compared in UTC; the tolerance must keep it."""
    assert _is_within_window(_paper("2026-07-22T23:00:00+00:00"), CUTOFF, NOW)


def test_beyond_tolerance_is_dropped():
    assert not _is_within_window(_paper("2026-07-24T13:00:00+00:00"), CUTOFF, NOW)


def test_unparseable_date_is_kept():
    assert _is_within_window(_paper("not-a-date"), CUTOFF, NOW)
