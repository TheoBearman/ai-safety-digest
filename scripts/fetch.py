#!/usr/bin/env python3
"""
Main orchestrator: fetch papers from all sources, deduplicate, filter for
research relevance, enrich abstracts, and write to data/papers.json.

Usage (from project root):
    python scripts/fetch.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path so that ``scripts.*`` imports work
# when the script is executed directly.
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.models import load_config, Paper
from scripts.fetchers.rss import fetch_rss
from scripts.fetchers.scraper import fetch_scraped
from scripts.fetchers.lesswrong import fetch_lesswrong
from scripts.fetchers.trending import fetch_trending
from scripts.dedup import deduplicate
from scripts.enrich import enrich_abstracts
from scripts.filter import filter_papers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.yaml")
OUTPUT_PATH = os.path.join(PROJECT_ROOT, "data", "papers.json")

MAX_ABSTRACT_WORDS = 150

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _clean_abstract(text: str) -> str:
    """Strip HTML, collapse whitespace, and cap at MAX_ABSTRACT_WORDS."""
    if not text:
        return ""
    text = _HTML_TAG_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    # Remove common date-prefix boilerplate from RSS/scraped content
    text = re.sub(
        r"^Published on [A-Z][a-z]+ \d{1,2}, \d{4}\s*\d*:?\d*\s*[AP]?M?\s*GMT\s*",
        "", text,
    )
    text = re.sub(
        r"^(?:Blog|Research|Report|Paper)\s+[\w\s&]+\u2022\s*"
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},?\s+\d{4}\s*",
        "", text,
    )
    text = re.sub(
        r"^\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\s*",
        "", text,
    )
    text = text.strip()
    words = text.split()
    if len(words) > MAX_ABSTRACT_WORDS:
        truncated = " ".join(words[:MAX_ABSTRACT_WORDS])
        last_period = truncated.rfind(". ")
        if last_period > len(truncated) * 0.5:
            return truncated[: last_period + 1]
        return truncated + "..."
    return text


def main() -> None:
    logger.info("Loading configuration from %s", CONFIG_PATH)
    config = load_config(CONFIG_PATH)

    # -- Fetch from each source ------------------------------------------------
    all_papers: list[Paper] = []

    for label, fetch_fn, cfg_key in [
        ("RSS feeds",   fetch_rss,       "rss_feeds"),
        ("scrapers",    fetch_scraped,   "scrapers"),
        ("LessWrong",   fetch_lesswrong, "lesswrong"),
        ("trending",    fetch_trending,  "trending"),
    ]:
        cfg = config.get(cfg_key, [] if cfg_key in ("rss_feeds", "scrapers") else {})
        if cfg:
            logger.info("Fetching %s", label)
            all_papers.extend(fetch_fn(cfg))

    logger.info("Total papers before processing: %d", len(all_papers))

    # -- Global date filter: keep only papers from the last 7 days -------------
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    date_filtered: list[Paper] = []
    for p in all_papers:
        try:
            pub_dt = datetime.fromisoformat(p.published_date)
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            if pub_dt >= cutoff:
                date_filtered.append(p)
        except (ValueError, TypeError):
            # If we can't parse the date, keep the paper
            date_filtered.append(p)
    removed = len(all_papers) - len(date_filtered)
    if removed:
        logger.info("Date filter: removed %d papers older than 7 days", removed)
    all_papers = date_filtered

    # -- Pipeline: dedup → filter → enrich → clean -----------------------------
    papers = deduplicate(all_papers)
    papers = filter_papers(papers)
    papers = enrich_abstracts(papers)

    for p in papers:
        p.abstract = _clean_abstract(p.abstract)

    papers.sort(key=lambda p: p.published_date, reverse=True)

    # -- Write output ----------------------------------------------------------
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump([p.to_dict() for p in papers], f, indent=2, ensure_ascii=False)

    logger.info("Wrote %d papers to %s", len(papers), OUTPUT_PATH)

    # -- Summary ---------------------------------------------------------------
    counts = Counter(p.source_type for p in papers)
    logger.info("--- Summary ---")
    for src in ("rss", "scrape"):
        logger.info("  %-8s %d papers", src, counts.get(src, 0))
    logger.info("  %-8s %d papers", "TOTAL", len(papers))


if __name__ == "__main__":
    main()
