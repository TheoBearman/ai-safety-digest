"""Fetch papers/articles from RSS and Atom feeds."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import feedparser

from scripts.models import Paper

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Research-focused default keywords that apply as a soft filter to ALL feeds.
# These are used when a feed has no explicit keywords configured.
# For known research orgs this is a warning-only (soft) filter; for others
# it's a hard skip.
# ---------------------------------------------------------------------------

_DEFAULT_RESEARCH_KEYWORDS = [
    "paper", "research", "study", "model", "training", "benchmark",
    "evaluation", "dataset", "algorithm", "framework", "architecture",
    "fine-tuning", "fine tuning", "finetuning", "rlhf",
    "interpretability", "mechanistic", "alignment", "probe", "ablation",
    "embedding", "transformer", "neural", "gradient", "loss",
    "optimization", "inference", "scaling", "emergent", "capability",
    "red team", "safety", "robustness", "adversarial", "reward model",
    "constitutional", "reinforcement learning", "language model",
    "technical report", "system card", "pretraining", "pre-training",
    "experiment", "methodology", "we propose", "we present", "we show",
    "we introduce", "we evaluate", "our method", "our approach",
    "survey", "analysis", "compute", "oversight", "jailbreak",
    "guardrail", "deception", "sycophancy", "corrigibility",
    "specification", "arxiv",
]

# Organizations whose feeds are known to be primarily research-oriented.
# For these, the default keyword filter logs a warning but does NOT skip.
_KNOWN_RESEARCH_FEED_ORGS = {
    "anthropic", "openai", "google deepmind", "microsoft research",
    "redwood research", "alignment forum", "dan hendrycks", "epoch ai",
    "fli", "lesswrong", "semianalysis", "zvi mowshowitz",
}

# Curated voices — skip ALL keyword filtering (both explicit and default).
# These authors primarily write about AI, so every post is included.
_ALWAYS_INCLUDE_FEED_ORGS = {
    "dean ball", "seb krier", "peter wildeford", "ajeya cotra",
}


# ---------------------------------------------------------------------------
# Date parsing helpers
# ---------------------------------------------------------------------------

def _parse_entry_date(entry) -> Optional[datetime]:
    """Try several strategies to extract a datetime from a feed entry."""
    # Strategy 1: use the pre-parsed struct_time that feedparser provides
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed is not None:
            try:
                return datetime.fromtimestamp(
                    time.mktime(parsed), tz=timezone.utc
                )
            except (OverflowError, OSError, ValueError):
                continue

    # Strategy 2: parse the raw date string with dateutil
    for attr in ("published", "updated"):
        raw = getattr(entry, attr, None)
        if raw:
            try:
                from dateutil.parser import parse as dateutil_parse
                return dateutil_parse(raw)
            except Exception:
                pass

    # Strategy 3: common manual formats
    for attr in ("published", "updated"):
        raw = getattr(entry, attr, None)
        if raw:
            for fmt in (
                "%a, %d %b %Y %H:%M:%S %z",
                "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d",
            ):
                try:
                    return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue

    return None


def _extract_author(entry, feed) -> list[str]:
    """Extract author name(s) from a feed entry."""
    # Some feeds (including many Substack feeds) put the author on the entry
    if hasattr(entry, "author") and entry.author:
        return [entry.author.strip()]

    # Atom-style <author><name>...</name></author>
    if hasattr(entry, "authors") and entry.authors:
        names = [a.get("name", "").strip() for a in entry.authors if a.get("name")]
        if names:
            return names

    # Fallback: use the feed-level title (common for single-author blogs)
    if hasattr(feed, "feed") and hasattr(feed.feed, "title") and feed.feed.title:
        return [feed.feed.title.strip()]

    return ["Unknown"]


def _extract_abstract(entry) -> str:
    """Extract the best available summary text from a feed entry."""
    # Prefer the explicit summary
    if hasattr(entry, "summary") and entry.summary:
        return entry.summary.strip()

    # Some feeds use content instead
    if hasattr(entry, "content") and entry.content:
        for content_item in entry.content:
            value = content_item.get("value", "")
            if value:
                return value.strip()

    # description is another common field
    if hasattr(entry, "description") and entry.description:
        return entry.description.strip()

    return ""


def _matches_keywords(title: str, abstract: str, keywords: list[str]) -> bool:
    """Return True if any keyword appears in the title or abstract."""
    searchable = (title + " " + abstract).lower()
    return any(kw in searchable for kw in keywords)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_rss(feeds_config: list[dict]) -> list[Paper]:
    """
    Fetch papers from a list of RSS/Atom feeds.

    Parameters
    ----------
    feeds_config : list[dict]
        Each dict must contain ``url`` and ``org``, and optionally ``name``
        and ``keywords``.

    Returns
    -------
    list[Paper]
        Papers published within the last 7 days.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    papers: list[Paper] = []

    for feed_cfg in feeds_config:
        feed_url = feed_cfg.get("url", "")
        org = feed_cfg.get("org", "Unknown")
        feed_name = feed_cfg.get("name", feed_url)
        explicit_keywords = [k.lower() for k in feed_cfg.get("keywords", [])]
        required_categories = [c.lower() for c in feed_cfg.get("categories", [])]
        is_research_org = org.lower() in _KNOWN_RESEARCH_FEED_ORGS
        is_always_include = org.lower() in _ALWAYS_INCLUDE_FEED_ORGS

        logger.info("Fetching RSS feed: %s (%s)", feed_name, feed_url)

        try:
            feed = feedparser.parse(feed_url)

            if feed.bozo and not feed.entries:
                logger.warning(
                    "Feed %s returned an error: %s",
                    feed_name,
                    getattr(feed, "bozo_exception", "unknown error"),
                )
                continue

            for entry in feed.entries:
                pub_date = _parse_entry_date(entry)

                # Skip entries without a parseable date or older than 7 days
                if pub_date is None:
                    logger.debug(
                        "Skipping entry '%s' -- could not parse date",
                        getattr(entry, "title", "untitled"),
                    )
                    continue

                # Ensure timezone-aware comparison
                if pub_date.tzinfo is None:
                    pub_date = pub_date.replace(tzinfo=timezone.utc)

                if pub_date < cutoff:
                    continue

                title = getattr(entry, "title", "Untitled").strip()
                link = getattr(entry, "link", "").strip()
                authors = _extract_author(entry, feed)
                abstract = _extract_abstract(entry)

                # ---------------------------------------------------------
                # Category filtering (RSS <category> tags)
                # ---------------------------------------------------------
                if required_categories:
                    entry_cats = [
                        c.term.strip() if hasattr(c, "term") else str(c).strip()
                        for c in getattr(entry, "tags", [])
                    ]
                    entry_cats_lower = [c.lower() for c in entry_cats]
                    if not any(rc in entry_cats_lower for rc in required_categories):
                        continue

                # ---------------------------------------------------------
                # Keyword filtering (two layers)
                # Curated voices bypass all keyword filtering.
                # ---------------------------------------------------------

                if not is_always_include:
                    # Layer 1: Explicit per-feed keywords (hard filter)
                    if explicit_keywords:
                        if not _matches_keywords(title, abstract, explicit_keywords):
                            continue

                    # Layer 2: Default research keywords (applies to ALL feeds)
                    # For known research orgs this is a soft filter (warn only).
                    # For other feeds this is a hard skip.
                    if not explicit_keywords and not required_categories:
                        if not _matches_keywords(title, abstract, _DEFAULT_RESEARCH_KEYWORDS):
                            if is_research_org:
                                logger.warning(
                                    "Feed %s: entry '%s' did not match default "
                                    "research keywords but is from known research "
                                    "org — keeping with warning",
                                    feed_name,
                                    title,
                                )
                            else:
                                logger.info(
                                    "Feed %s: skipping entry '%s' — no research "
                                    "keyword match",
                                    feed_name,
                                    title,
                                )
                                continue

                papers.append(
                    Paper(
                        title=title,
                        authors=authors,
                        organization=org,
                        abstract=abstract,
                        url=link,
                        published_date=pub_date.isoformat(),
                        source_type="rss",
                        source_url=feed_url,
                    )
                )

            logger.info(
                "Feed %s: processed %d entries", feed_name, len(feed.entries)
            )

        except Exception:
            logger.warning("Failed to fetch feed %s", feed_name, exc_info=True)
            continue

    logger.info("RSS total: %d papers collected", len(papers))
    return papers
