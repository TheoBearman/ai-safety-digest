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
import math
import os
import re
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

    week_start is the most recent Monday at or before *reference*,
    week_end is the following Sunday.
    """
    if reference is None:
        reference = datetime.now()
    # Monday = 0 in weekday()
    monday = reference - timedelta(days=reference.weekday())
    sunday = monday + timedelta(days=6)
    fmt = "%B %d, %Y"
    return monday.strftime(fmt), sunday.strftime(fmt)


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

# ---------------------------------------------------------------------------
# Featured-paper scoring
# ---------------------------------------------------------------------------

# Minimum score a paper must reach to be shown in the hero section.
FEATURED_MIN_SCORE: float = 12.0

# Maximum number of papers in the hero section.
FEATURED_MAX_COUNT: int = 3

# Terms in a title that signal substantial research (case-insensitive).
_RESEARCH_TITLE_RE: re.Pattern[str] = re.compile(
    r"\b(?:"
    r"paper|model|benchmark|evaluat\w+|framework|alignment|safety|"
    r"interpretab\w+|reward|reinforcement|fine[- ]?tun\w+|train\w+|"
    r"scal\w+|language model|LLM|agent|auditing|red[- ]?team\w+|"
    r"monitor\w+|oversight|robustness|jailbreak|watermark\w+|"
    r"decepti\w+|mesa[- ]?optim\w+|corrigib\w+|specification|"
    r"governance|regulation|risk|catastroph\w+|existential|"
    r"superint\w+|capability|dangerous|dual[- ]?use|biosecurity|"
    r"cyber|verification|detect\w+|mitigat\w+"
    r")\b",
    re.IGNORECASE,
)


def _parse_date(date_str: str) -> datetime | None:
    """Parse an ISO 8601 date string into a naive datetime, or None."""
    if not date_str:
        return None
    try:
        if "T" in str(date_str):
            return datetime.fromisoformat(
                str(date_str).replace("Z", "+00:00")
            ).replace(tzinfo=None)
        return datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def score_paper(paper: dict, now: datetime | None = None) -> float:
    """Score a paper for featured hero-section selection.

    The score is a float composed of several weighted signals:

    1. Source authority (0-20 pts)
       - TOP_TIER_ORGS:  +20
       - PRIORITY_ORGS (not top-tier): +12
       - Other named orgs (not community): +5
       - COMMUNITY_ORGS: +0

    2. Content quality (0-10 pts)
       - Abstract length:  scaled 0-5 based on character count (0-500+ chars)
       - Research-y title: +3 if title matches research term patterns
       - Named authors:    +2 if at least one non-empty, non-"Unknown" author

    3. Recency (0-10 pts)
       - Exponential decay: 10 * exp(-0.2 * days_ago)
       - 0 days ago -> 10, 1 day -> ~8.2, 3 days -> ~5.5, 7 days -> ~2.5

    4. Penalties
       - Community org:    -5 (on top of getting 0 from authority)
       - No abstract:      -3
    """
    if now is None:
        now = datetime.now()

    score: float = 0.0
    org = paper.get("organization", "") or ""
    abstract = paper.get("abstract", "") or ""
    title = paper.get("title", "") or ""
    authors = paper.get("authors", []) or []
    source_type = paper.get("source_type", "") or ""

    # ----- 1. Source authority -----
    if org in TOP_TIER_ORGS:
        score += 20.0
    elif org in PRIORITY_ORGS:
        score += 12.0
    elif org and org not in COMMUNITY_ORGS:
        score += 5.0
    # Community orgs get 0 for authority, plus a penalty below.

    # ----- 2. Content quality signals -----

    # Abstract length: scale from 0 to 5 based on length.
    # 500+ characters earns the full 5 points.
    abstract_len = len(abstract)
    score += min(abstract_len / 100.0, 5.0)

    # Research-y title terms
    if _RESEARCH_TITLE_RE.search(title):
        score += 3.0

    # Named authors (at least one real name)
    has_real_author = any(
        a and a.strip() and a.strip().lower() != "unknown"
        for a in authors
    )
    if has_real_author:
        score += 2.0

    # ----- 3. Recency (smooth exponential decay) -----
    pub_date = _parse_date(paper.get("published_date", ""))
    if pub_date is not None:
        days_ago = max((now - pub_date).total_seconds() / 86400.0, 0.0)
        # Exponential decay: half-life of ~3.5 days
        score += 10.0 * math.exp(-0.2 * days_ago)
    # No date at all -> no recency bonus (0 out of 10)

    # ----- 4. Penalties -----
    if org in COMMUNITY_ORGS:
        score -= 5.0

    if abstract_len < 20:
        score -= 3.0

    return round(score, 2)


def select_featured(
    papers: list[dict],
    now: datetime | None = None,
    max_count: int = FEATURED_MAX_COUNT,
    min_score: float = FEATURED_MIN_SCORE,
) -> list[dict]:
    """Select up to *max_count* papers for the featured hero section.

    Steps:
    1. Score every paper.
    2. Sort by descending score (stable index as tiebreaker).
    3. Apply the minimum score threshold.
    4. Enforce organizational diversity: the featured set should not contain
       more than one paper from the same organization. After placing the
       top-scoring paper, each subsequent slot goes to the next-highest
       paper whose organization has not already been selected. This ensures
       the hero section showcases breadth across the field.

    Returns a list of up to *max_count* paper dicts.
    """
    if now is None:
        now = datetime.now()

    scored: list[tuple[float, int, dict]] = [
        (score_paper(p, now), idx, p) for idx, p in enumerate(papers)
    ]
    # Sort: highest score first, then by original index for stability
    scored.sort(key=lambda t: (-t[0], t[1]))

    featured: list[dict] = []
    seen_orgs: set[str] = set()

    for paper_score, _idx, paper in scored:
        if len(featured) >= max_count:
            break
        if paper_score < min_score:
            break  # All remaining papers are below the threshold

        org = paper.get("organization", "") or ""
        if org and org in seen_orgs:
            continue  # Skip duplicate org to maintain diversity

        featured.append(paper)
        if org:
            seen_orgs.add(org)

    return featured


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


def render(papers: list[dict], css: str) -> str:
    """Render the Jinja2 template with the provided data and return HTML."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("index.html.j2")

    now = datetime.now()
    week_start, week_end = compute_week_range(now)
    fetch_date = now.strftime("%Y-%m-%d")
    last_updated = now.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    organizations = extract_organizations(papers)

    # Sort by published date, newest first
    papers = sorted(papers, key=lambda p: p.get("published_date", ""), reverse=True)

    # --- Hero section: select featured papers with the new algorithm ---
    featured_papers = select_featured(papers, now)
    featured_urls = {p.get("url") for p in featured_papers}
    grid_papers = [p for p in papers if p.get("url") not in featured_urls]
    total_count = len(papers)

    html = template.render(
        papers=grid_papers,
        featured_papers=featured_papers,
        total_count=total_count,
        week_start=week_start,
        week_end=week_end,
        fetch_date=fetch_date,
        last_updated=last_updated,
        organizations=organizations,
        css=css,
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
