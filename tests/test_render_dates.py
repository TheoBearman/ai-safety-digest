"""Date-display tests for scripts.render: month-precision papers must not
show a fabricated exact day."""

from __future__ import annotations

from datetime import datetime

from scripts.render import render

FROZEN_NOW = datetime(2026, 7, 22, 8, 30, 0)


def _paper(**overrides) -> dict:
    base = {
        "title": "Month Precision Paper",
        "authors": [],
        "organization": "Google DeepMind",
        "abstract": "An abstract that is long enough to display sensibly.",
        "url": "https://example.com/paper",
        "published_date": "2026-07-22T00:00:00+00:00",
        "source_type": "scrape",
        "source_url": "https://example.com",
    }
    base.update(overrides)
    return base


def _render(paper: dict) -> str:
    return render(papers=[paper], css="", now=FROZEN_NOW, health=[])


def test_month_precision_renders_month_year_only():
    html = _render(_paper(date_precision="month"))
    assert 'paper-date">Jul 2026</span>' in html
    assert 'paper-date">Jul 22, 2026</span>' not in html


def test_day_precision_renders_full_date():
    html = _render(_paper(date_precision="day"))
    assert 'paper-date">Jul 22, 2026</span>' in html


def test_missing_precision_defaults_to_full_date():
    html = _render(_paper())
    assert 'paper-date">Jul 22, 2026</span>' in html
