#!/usr/bin/env python3
"""
render.py — Renders the AI Safety Digest static site.

Loads paper data from data/papers.json, applies the Jinja2 template,
inlines the CSS, and writes the final HTML to site/index.html.

Usage:
    python scripts/render.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from jinja2 import Environment, FileSystemLoader

# ---------------------------------------------------------------------------
# Resolve project root (one level up from scripts/)
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

# Ensure the project root is on sys.path so sibling modules can be imported
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DATA_FILE = os.path.join(PROJECT_ROOT, "data", "papers.json")
TEMPLATE_DIR = os.path.join(PROJECT_ROOT, "templates")
CSS_FILE = os.path.join(PROJECT_ROOT, "static", "style.css")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "site")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "index.html")


def load_papers(path: str) -> list[dict]:
    """Load papers from a JSON file. Returns an empty list if the file
    is missing or contains invalid JSON."""
    if not os.path.isfile(path):
        print(f"[render] Warning: {path} not found. Using empty paper list.")
        return []
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data
    # Support a top-level wrapper like {"papers": [...]}
    if isinstance(data, dict) and "papers" in data:
        return data["papers"]
    print("[render] Warning: Unexpected JSON structure. Using empty paper list.")
    return []


def load_css(path: str) -> str:
    """Read the CSS file and return its contents as a string."""
    if not os.path.isfile(path):
        print(f"[render] Warning: {path} not found. No CSS will be inlined.")
        return ""
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def compute_week_range(reference: datetime | None = None) -> tuple[str, str]:
    """Return (week_start, week_end) formatted as 'Month Day, Year'.

    week_start is 7 days before *reference*, week_end is *reference*.
    This matches the 7-day data window used by the fetchers.
    """
    if reference is None:
        reference = datetime.now()
    week_start = reference - timedelta(days=7)
    fmt = "%B %d, %Y"
    return week_start.strftime(fmt), reference.strftime(fmt)


# ---------------------------------------------------------------------------
# Organization tiers for scoring and sorting
# ---------------------------------------------------------------------------

# Highest tier: top-tier AI labs and dedicated safety research organizations.
TOP_TIER_ORGS: set[str] = {
    "Anthropic",
    "OpenAI",
    "Google DeepMind",
    "Microsoft Research",
    "Redwood Research",
    "ARC",
    "MIRI",
    "CAIS",
    "Apollo Research",
    "METR",
    "UK AISI",
    "US AISI",
}

# Priority orgs: respected policy/research institutions and notable researchers.
# These score well but below TOP_TIER_ORGS.
PRIORITY_ORGS: list[str] = [
    "Anthropic",
    "OpenAI",
    "Google DeepMind",
    "UK AISI",
    "US AISI",
    "CAIS",
    "METR",
    "ARC",
    "Redwood Research",
    "Apollo Research",
    "MIRI",
    "Microsoft Research",
    "FAR AI",
    "MATS",
    "GovAI",
    "IAPS",
    "CSET",
    "Yoshua Bengio",
    "Lennart Heim",
    "SemiAnalysis",
    "Zvi Mowshowitz",
    "Dean Ball",
    "Seb Krier",
    "Peter Wildeford",
    "Ajeya Cotra",
    "Jack Clark",
    "Helen Toner",
    "CNAS",
    "Forethought",
]

# Community/aggregator sources: useful for the grid but should not dominate
# the featured hero section since they surface others' work, not primary research.
COMMUNITY_ORGS: set[str] = {
    "Reddit",
    "Hacker News",
    "LessWrong",
    "Alignment Forum",
    "Astral Codex Ten",
    "Zvi Mowshowitz",
    "Import AI",
    "Vox Future Perfect",
}


def extract_organizations(papers: list[dict]) -> list[str]:
    """Return a sorted list of unique organization names from the papers,
    with priority orgs listed first."""
    orgs: set[str] = set()
    for paper in papers:
        org = paper.get("organization")
        if org:
            orgs.add(org)
    # Priority orgs first, then alphabetical
    priority = [o for o in PRIORITY_ORGS if o in orgs]
    rest = sorted(o for o in orgs if o not in PRIORITY_ORGS)
    return priority + rest


def _load_health(now: datetime) -> tuple[list[dict], int]:
    """Return (sorted_health_list, broken_count).

    Imports observability lazily so render.py stays usable even if the
    observability module hasn't been touched yet (e.g. in fixture-driven tests).
    """
    try:
        from scripts.observability import compute_health
    except Exception:
        return [], 0

    health = compute_health()
    if not health:
        return [], 0

    # Order: broken first, degraded, healthy, unknown — most-actionable first.
    status_order = {"broken": 0, "degraded": 1, "healthy": 2, "unknown": 3}
    items = []
    for src in health.values():
        items.append({
            "name": src.name,
            "status": src.status,
            "last_success_at": src.last_success_at,
            "last_run_at": src.last_run_at,
            "last_items_fetched": src.last_items_fetched,
            "last_error": src.last_error,
        })
    items.sort(key=lambda h: (status_order.get(h["status"], 99), h["name"].lower()))
    broken = sum(1 for h in items if h["status"] == "broken")
    return items, broken


def render(
    papers: list[dict],
    css: str,
    now: datetime | None = None,
    health: list[dict] | None = None,
    broken_count: int | None = None,
) -> str:
    """Render the Jinja2 template with the provided data and return HTML.

    Parameters
    ----------
    papers:
        Paper dicts.
    css:
        Stylesheet contents to inline.
    now:
        Optional clock injection for deterministic snapshot tests.
    health:
        Optional pre-computed health list. If omitted, the run log is read
        and ``compute_health()`` is used.
    broken_count:
        Optional pre-computed count of broken sources. Pairs with ``health``.
    """
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("index.html.j2")

    if now is None:
        now = datetime.now()
    week_start, week_end = compute_week_range(now)
    fetch_date = now.strftime("%Y-%m-%d")
    last_updated = now.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    organizations = extract_organizations(papers)

    # Sort by published date, newest first
    papers = sorted(papers, key=lambda p: p.get("published_date", ""), reverse=True)

    grid_papers = papers
    total_count = len(papers)

    if health is None:
        health, broken_count = _load_health(now)
    elif broken_count is None:
        broken_count = sum(1 for h in health if h.get("status") == "broken")

    html = template.render(
        papers=grid_papers,
        total_count=total_count,
        week_start=week_start,
        week_end=week_end,
        fetch_date=fetch_date,
        last_updated=last_updated,
        organizations=organizations,
        css=css,
        health=health,
        broken_count=broken_count,
    )
    return html


def main() -> None:
    print(f"[render] Project root: {PROJECT_ROOT}")
    print(f"[render] Loading papers from {DATA_FILE}")
    papers = load_papers(DATA_FILE)
    print(f"[render] Loaded {len(papers)} paper(s).")

    css = load_css(CSS_FILE)

    html = render(papers, css)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"[render] Wrote {OUTPUT_FILE} ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
