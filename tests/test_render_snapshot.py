"""Snapshot test for ``scripts.render.render``.

Renders a deterministic mini-corpus + health fixture and byte-compares against
``tests/snapshots/index.html``. Regenerate with ``UPDATE_SNAPSHOTS=1 pytest``.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from scripts.render import render

SNAPSHOT_PATH = os.path.join(
    os.path.dirname(__file__), "snapshots", "index.html"
)

# Frozen clock for deterministic rendering.
FROZEN_NOW = datetime(2026, 5, 11, 8, 30, 0)


def _normalize(text: str) -> str:
    """Normalize CRLF -> LF and strip trailing whitespace."""
    return text.replace("\r\n", "\n").rstrip() + "\n"


def test_render_matches_snapshot(fixture_papers, fixture_health, project_css):
    html = render(
        papers=fixture_papers,
        css=project_css,
        now=FROZEN_NOW,
        health=fixture_health,
    )
    rendered = _normalize(html)

    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
        with open(SNAPSHOT_PATH, "w", encoding="utf-8", newline="\n") as f:
            f.write(rendered)
        pytest.skip(
            f"UPDATE_SNAPSHOTS=1 — wrote new snapshot to {SNAPSHOT_PATH}"
        )

    if not os.path.isfile(SNAPSHOT_PATH):
        pytest.fail(
            f"Snapshot missing at {SNAPSHOT_PATH}. "
            f"Run with UPDATE_SNAPSHOTS=1 to create it."
        )

    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        expected = _normalize(f.read())

    if rendered != expected:
        # Write a debug copy next to the snapshot so the diff is inspectable.
        debug_path = SNAPSHOT_PATH + ".actual"
        with open(debug_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(rendered)
        pytest.fail(
            "Rendered HTML did not match snapshot. "
            f"Actual output written to {debug_path}. "
            "If the change is intentional, re-run with UPDATE_SNAPSHOTS=1."
        )


def test_render_includes_every_paper_exactly_once(
    fixture_papers, fixture_health, project_css
):
    """Every paper must reach the page exactly once.

    The featured/hero section is currently commented out in the template. When
    ``render`` also excluded featured papers from the grid, the top-scoring
    papers were selected and then rendered nowhere — silently dropped. The
    snapshot test could not catch it, because the truncated output *was* the
    blessed snapshot. This asserts the invariant directly, and also fails if a
    re-enabled hero renders featured papers twice.
    """
    html = render(
        papers=fixture_papers,
        css=project_css,
        now=FROZEN_NOW,
        health=fixture_health,
    )

    # One card per paper: too few means papers were dropped, too many means a
    # re-enabled hero is rendering the featured ones a second time.
    assert html.count('class="paper-card') == len(fixture_papers)

    # A card count alone would still pass if one paper were dropped while
    # another was duplicated, so check each paper actually reached the page.
    for paper in fixture_papers:
        assert paper["url"] in html, f"{paper['url']} was not rendered"


def test_render_reports_count_matching_rendered_cards(
    fixture_papers, fixture_health, project_css
):
    """The advertised total must match what is actually on the page."""
    html = render(
        papers=fixture_papers,
        css=project_css,
        now=FROZEN_NOW,
        health=fixture_health,
    )
    assert f">{len(fixture_papers)}</span>" in html


def test_render_handles_no_health(fixture_papers, project_css):
    """Without a run log present, render() should still succeed."""
    html = render(
        papers=fixture_papers,
        css=project_css,
        now=FROZEN_NOW,
        health=[],
        broken_count=0,
    )
    assert "<body" in html.lower()
    # The class is defined in the inlined CSS regardless; what we care about
    # is that the actual <details id="pipeline-health"> element is absent.
    assert 'id="pipeline-health"' not in html
    assert 'class="health-banner"' not in html


def test_render_shows_banner_when_broken(
    fixture_papers, fixture_health, project_css
):
    html = render(
        papers=fixture_papers,
        css=project_css,
        now=FROZEN_NOW,
        health=fixture_health,
    )
    assert 'class="health-banner"' in html
    assert "See details" in html
