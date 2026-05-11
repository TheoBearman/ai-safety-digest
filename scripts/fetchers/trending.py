"""Fetch trending AI safety content from Hacker News and Reddit."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import requests

from scripts.models import Paper

if TYPE_CHECKING:
    from scripts.observability import RunRecorder

logger = logging.getLogger(__name__)

HN_SEARCH_ENDPOINT = "https://hn.algolia.com/api/v1/search"

REQUEST_TIMEOUT = 5  # seconds

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; AISafetyDigestBot/1.0; "
        "+https://github.com/ai-safety-digest)"
    ),
}

# ---------------------------------------------------------------------------
# Research URL / domain filtering
# ---------------------------------------------------------------------------

# Domains that are known to host research papers and technical content
_RESEARCH_DOMAINS = {
    "arxiv.org",
    "openreview.net",
    "proceedings.mlr.press",
    "papers.nips.cc",
    "proceedings.neurips.cc",
    "aclanthology.org",
    "dl.acm.org",
    "ieeexplore.ieee.org",
    "scholar.google.com",
    "semanticscholar.org",
    "biorxiv.org",
    "medrxiv.org",
    "ssrn.com",
    "nature.com",
    "science.org",
    "pnas.org",
    # Research org blogs
    "anthropic.com",
    "openai.com",
    "deepmind.google",
    "deepmind.com",
    "microsoft.com",
    "research.google",
    "ai.google",
    "ai.meta.com",
    "redwoodresearch.org",
    "alignmentforum.org",
    "lesswrong.com",
    "safe.ai",
    "metr.org",
    "apolloresearch.ai",
    "alignment.org",
    "intelligence.org",
    "far.ai",
    "aisi.gov.uk",
    "governance.ai",
    "epochai.org",
    "futureoflife.org",
    "humancompatible.ai",
    "transformer-circuits.pub",
}

# Keywords in HN/Reddit titles that suggest research rather than news/opinion
_RESEARCH_TITLE_KEYWORDS = [
    "paper", "research", "study", "arxiv", "model", "training",
    "benchmark", "evaluation", "alignment", "interpretability", "safety",
    "dataset", "neural", "transformer", "rlhf", "fine-tuning",
    "fine tuning", "scaling", "architecture", "framework", "algorithm",
    "preprint", "survey", "technical report", "system card",
    "experiment", "ablation", "probe", "mechanistic", "reward model",
    "adversarial", "robustness", "jailbreak", "red team",
]

# Title patterns that strongly suggest news/opinion rather than research
_NEWS_TITLE_PATTERNS = [
    "announce", "launches", "launched", "raises", "funding",
    "acquires", "acquired", "ipo", "valuation", "billion",
    "million dollar", "stock", "shares", "layoff", "fired",
    "hired", "ceo", "cto", "executive", "podcast", "interview",
    "newsletter", "weekly", "roundup", "digest", "recap",
    "opinion", "editorial", "commentary",
]


def _is_research_url(url: str) -> bool:
    """Check if a URL points to a known research domain."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        # Check against known research domains
        for domain in _RESEARCH_DOMAINS:
            if hostname == domain or hostname.endswith("." + domain):
                return True
    except Exception:
        pass
    return False


def _has_research_title_keywords(title: str) -> bool:
    """Check if a title contains research-related keywords."""
    title_lower = title.lower()
    return any(kw in title_lower for kw in _RESEARCH_TITLE_KEYWORDS)


def _has_news_title_patterns(title: str) -> bool:
    """Check if a title looks like news/opinion rather than research."""
    title_lower = title.lower()
    return any(pat in title_lower for pat in _NEWS_TITLE_PATTERNS)


def _is_research_content(title: str, url: str) -> bool:
    """
    Determine if a HN story or Reddit post is likely research content.

    Returns True if the content should be kept.
    A story passes if:
      - Its URL points to a known research domain, OR
      - Its title contains research keywords and does NOT look like news.
    """
    # Known research domains always pass
    if _is_research_url(url):
        return True

    # If the title has research keywords and doesn't look like news, keep it
    if _has_research_title_keywords(title) and not _has_news_title_patterns(title):
        return True

    # If the title clearly looks like news, skip
    if _has_news_title_patterns(title):
        return False

    # Default: skip — we want to be conservative and only surface research
    return False


# ---------------------------------------------------------------------------
# Hacker News helpers
# ---------------------------------------------------------------------------

def _fetch_hn_for_query(
    query: str,
    min_points: int,
    cutoff_timestamp: int,
    hits_per_page: int = 10,
) -> list[dict]:
    """
    Search Hacker News via the Algolia API for a single query string.

    Returns the raw list of hit dicts from the API response.
    """
    params = {
        "query": query,
        "tags": "story",
        "numericFilters": f"points>{min_points},created_at_i>{cutoff_timestamp}",
        "hitsPerPage": hits_per_page,
    }

    logger.info("HN search: query=%r min_points=%d", query, min_points)

    try:
        response = requests.get(
            HN_SEARCH_ENDPOINT,
            params=params,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("hits", [])
    except requests.RequestException:
        logger.warning("Failed to search HN for %r", query, exc_info=True)
    except (KeyError, ValueError):
        logger.warning("Failed to parse HN response for %r", query, exc_info=True)

    return []


def _hn_hit_to_paper(hit: dict) -> Paper:
    """Convert a single Algolia HN hit dict into a Paper."""
    title = (hit.get("title") or "").strip()
    url = (hit.get("url") or "").strip()
    object_id = hit.get("objectID", "")

    # If the story has no external URL, link to the HN discussion
    if not url:
        url = f"https://news.ycombinator.com/item?id={object_id}"

    author = (hit.get("author") or "").strip()
    points = hit.get("points", 0)
    num_comments = hit.get("num_comments", 0)

    created_at = hit.get("created_at", "")
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            published_date = dt.isoformat()
        except (ValueError, AttributeError):
            published_date = datetime.now(timezone.utc).isoformat()
    else:
        published_date = datetime.now(timezone.utc).isoformat()

    abstract = f"Hacker News discussion with {points} points and {num_comments} comments."

    return Paper(
        title=title,
        authors=[author] if author else ["Unknown"],
        organization="Hacker News",
        abstract=abstract,
        url=url,
        published_date=published_date,
        source_type="rss",
        source_url="https://news.ycombinator.com/",
    )


def _fetch_hn(config: dict, cutoff: datetime) -> list[Paper]:
    """
    Fetch trending AI safety stories from Hacker News.

    Returns a deduplicated list of Paper objects, filtered for research content.
    """
    queries: list[str] = config.get(
        "hn_queries", ["AI safety", "AI alignment", "mechanistic interpretability"]
    )
    min_points: int = config.get("hn_min_points", 50)
    hn_keywords: list[str] = [
        k.lower() for k in config.get("hn_keywords", [])
    ]
    cutoff_timestamp = int(cutoff.timestamp())

    seen_ids: set[str] = set()
    papers: list[Paper] = []

    for query in queries:
        hits = _fetch_hn_for_query(query, min_points, cutoff_timestamp)

        for hit in hits:
            object_id = hit.get("objectID", "")
            if object_id in seen_ids:
                continue
            seen_ids.add(object_id)

            title = (hit.get("title") or "").strip()
            if not title:
                continue

            url = (hit.get("url") or "").strip()

            # Filter: skip stories that are not research content
            if not _is_research_content(title, url):
                logger.info(
                    "HN: skipping non-research story: '%s' (%s)", title, url
                )
                continue

            # Additional keyword filter from config (if configured)
            if hn_keywords:
                title_lower = title.lower()
                if not any(kw in title_lower for kw in hn_keywords):
                    # Check URL too
                    url_lower = (url or "").lower()
                    if not any(kw in url_lower for kw in hn_keywords):
                        logger.info(
                            "HN: skipping story without keyword match: '%s'",
                            title,
                        )
                        continue

            papers.append(_hn_hit_to_paper(hit))

    logger.info("Hacker News: %d stories collected", len(papers))
    return papers


# ---------------------------------------------------------------------------
# Reddit helpers
# ---------------------------------------------------------------------------

def _fetch_subreddit(subreddit: str) -> list[dict]:
    """
    Fetch the top posts from a subreddit for the past week.

    Returns the list of post data dicts from Reddit's JSON API.
    """
    url = f"https://www.reddit.com/r/{subreddit}/top/.json"
    params = {"t": "week", "limit": 10}

    logger.info("Reddit: fetching r/%s top posts", subreddit)

    try:
        response = requests.get(
            url,
            params=params,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        children = data.get("data", {}).get("children", [])
        return [child.get("data", {}) for child in children]
    except requests.RequestException:
        logger.warning("Failed to fetch r/%s", subreddit, exc_info=True)
    except (KeyError, ValueError):
        logger.warning("Failed to parse r/%s response", subreddit, exc_info=True)

    return []


def _reddit_post_to_paper(post: dict, subreddit: str) -> Paper:
    """Convert a single Reddit post data dict into a Paper."""
    title = (post.get("title") or "").strip()
    author = (post.get("author") or "").strip()
    score = post.get("score", 0)
    selftext = (post.get("selftext") or "").strip()
    permalink = post.get("permalink", "")
    external_url = (post.get("url") or "").strip()

    # For link posts, use the external URL; for self posts, use Reddit link
    if external_url and not post.get("is_self", False):
        url = external_url
    elif permalink:
        url = f"https://www.reddit.com{permalink}"
    else:
        url = f"https://www.reddit.com/r/{subreddit}/"

    # Use selftext as abstract, truncated
    if selftext:
        abstract = selftext[:500].rsplit(" ", 1)[0] + "..." if len(selftext) > 500 else selftext
    else:
        abstract = f"Reddit r/{subreddit} post with {score} upvotes."

    created_utc = post.get("created_utc", 0)
    if created_utc:
        dt = datetime.fromtimestamp(created_utc, tz=timezone.utc)
        published_date = dt.isoformat()
    else:
        published_date = datetime.now(timezone.utc).isoformat()

    return Paper(
        title=title,
        authors=[author] if author else ["Unknown"],
        organization="Reddit",
        abstract=abstract,
        url=url,
        published_date=published_date,
        source_type="rss",
        source_url=f"https://www.reddit.com/r/{subreddit}/",
    )


def _fetch_reddit(config: dict) -> list[Paper]:
    """
    Fetch trending AI safety posts from configured subreddits.

    Filters out self-posts that are pure discussion (no link to research)
    and posts whose titles/URLs don't indicate research content.

    Returns a list of Paper objects.
    """
    subreddits: list[str] = config.get("subreddits", ["aisafety", "mlsafety"])
    min_score: int = config.get("reddit_min_score", 0)
    papers: list[Paper] = []

    for subreddit in subreddits:
        posts = _fetch_subreddit(subreddit)

        for post in posts:
            title = (post.get("title") or "").strip()
            if not title:
                continue

            # Skip posts below minimum score threshold
            score = post.get("score", 0)
            if min_score and score < min_score:
                logger.info(
                    "Reddit: skipping low-score post (%d < %d): '%s'",
                    score, min_score, title,
                )
                continue

            is_self = post.get("is_self", False)
            external_url = (post.get("url") or "").strip()

            # Skip self-posts that are just discussion (no external link)
            if is_self:
                # Self posts are typically discussion, questions, opinions.
                # Only keep them if the title strongly suggests research.
                if not _has_research_title_keywords(title):
                    logger.info(
                        "Reddit: skipping self-post (discussion): '%s'", title
                    )
                    continue

            # For link posts, check if the URL points to research
            if not is_self and external_url:
                if not _is_research_content(title, external_url):
                    logger.info(
                        "Reddit: skipping non-research link post: '%s' (%s)",
                        title,
                        external_url,
                    )
                    continue

            papers.append(_reddit_post_to_paper(post, subreddit))

    logger.info("Reddit: %d posts collected", len(papers))
    return papers


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_trending(config: dict, recorder=None) -> list[Paper]:
    """
    Fetch trending AI safety content from Hacker News and Reddit.

    Parameters
    ----------
    config : dict
        May contain ``hn_queries``, ``hn_min_points``, ``hn_keywords``,
        ``subreddits``, and ``days_back``.
    recorder : RunRecorder, optional

    Returns
    -------
    list[Paper]
    """
    days_back: int = config.get("days_back", 7)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    papers: list[Paper] = []

    # Hacker News (aggregate across queries — one source record)
    hn_start = time.perf_counter()
    hn_status = "ok"
    hn_error: str | None = None
    try:
        hn_papers = _fetch_hn(config, cutoff)
        papers.extend(hn_papers)
    except Exception as exc:
        logger.warning("Hacker News fetch failed", exc_info=True)
        hn_status = "error"
        hn_error = f"{type(exc).__name__}: {exc}"
        hn_papers = []
    if recorder is not None and config.get("hn_queries"):
        recorder.record_source(
            name="Hacker News",
            org="Hacker News",
            type="trending",
            items_fetched=len(hn_papers),
            duration_seconds=time.perf_counter() - hn_start,
            status=hn_status,
            error=hn_error,
        )

    # Reddit (one record per subreddit so a single broken sub is visible)
    subreddits: list[str] = config.get("subreddits", [])
    min_score: int = config.get("reddit_min_score", 0)
    for subreddit in subreddits:
        sub_start = time.perf_counter()
        sub_status = "ok"
        sub_error: str | None = None
        sub_items_before = len(papers)
        try:
            sub_papers = _fetch_reddit({
                "subreddits": [subreddit],
                "reddit_min_score": min_score,
            })
            papers.extend(sub_papers)
        except Exception as exc:
            logger.warning("Reddit r/%s fetch failed", subreddit, exc_info=True)
            sub_status = "error"
            sub_error = f"{type(exc).__name__}: {exc}"
        if recorder is not None:
            recorder.record_source(
                name=f"Reddit r/{subreddit}",
                org="Reddit",
                type="trending",
                items_fetched=len(papers) - sub_items_before,
                duration_seconds=time.perf_counter() - sub_start,
                status=sub_status,
                error=sub_error,
            )

    logger.info("Trending total: %d items collected", len(papers))
    return papers
