"""Fetch high-karma posts from LessWrong using their GraphQL API."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import requests

from scripts.models import Paper

if TYPE_CHECKING:
    from scripts.observability import RunRecorder

logger = logging.getLogger(__name__)

GRAPHQL_ENDPOINT = "https://www.lesswrong.com/graphql"

REQUEST_TIMEOUT = 5  # seconds

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (compatible; AISafetyDigestBot/1.0; "
        "+https://github.com/ai-safety-digest)"
    ),
}

def _build_payload(after_date: str, limit: int) -> dict:
    """Return the GraphQL payload with inline parameters."""
    query = (
        '{ posts(input: { terms: { after: "%s", limit: %d, sortedBy: "top" } }) '
        '{ results { title pageUrl postedAt baseScore '
        'user { displayName } } } }'
    ) % (after_date, limit)
    return {"query": query, "variables": {}}


def _truncate(text: str, max_length: int = 500) -> str:
    """Truncate *text* to *max_length* characters, appending an ellipsis if needed."""
    if not text:
        return ""
    if len(text) <= max_length:
        return text
    return text[:max_length].rsplit(" ", 1)[0] + "..."


def _extract_abstract(post: dict) -> str:
    """Return the best available summary for a post."""
    for field in ("excerpt", "plaintextDescription"):
        text = (post.get(field) or "").strip()
        if text:
            return _truncate(text)

    score = post.get("baseScore", 0) or 0
    return f"LessWrong post with {score} karma."


def _parse_posted_date(post: dict) -> str:
    """Parse the postedAt field and return an ISO 8601 string."""
    raw = post.get("postedAt", "")
    if not raw:
        return datetime.now(timezone.utc).isoformat()

    try:
        # LessWrong typically returns ISO 8601 dates
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.isoformat()
    except (ValueError, AttributeError):
        pass

    return datetime.now(timezone.utc).isoformat()


def _post_url(post: dict) -> str:
    """Return the URL for a post, falling back to constructing one from the title."""
    page_url = (post.get("pageUrl") or "").strip()
    if page_url:
        # pageUrl may be a relative path like /posts/abc123/my-post-title
        if page_url.startswith("/"):
            return "https://www.lesswrong.com" + page_url
        return page_url

    return "https://www.lesswrong.com/"


def fetch_lesswrong(
    config: dict,
    recorder: "RunRecorder | None" = None,
) -> list[Paper]:
    """
    Fetch high-karma posts from LessWrong via their GraphQL API.

    Parameters
    ----------
    config : dict
        Must contain ``min_karma``, ``days_back``, and ``max_results``.
    recorder : RunRecorder, optional

    Returns
    -------
    list[Paper]
    """
    min_karma: int = config.get("min_karma", 150)
    days_back: int = config.get("days_back", 7)
    max_results: int = config.get("max_results", 20)

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    after_date = cutoff.strftime("%Y-%m-%d")

    payload = _build_payload(after_date, max_results)

    logger.info(
        "LessWrong: fetching posts with karma >= %d from last %d days",
        min_karma,
        days_back,
    )

    papers: list[Paper] = []
    start = time.perf_counter()
    status = "ok"
    error: str | None = None

    try:
        response = requests.post(
            GRAPHQL_ENDPOINT,
            json=payload,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        posts = (
            data.get("data", {})
            .get("posts", {})
            .get("results", [])
        )

        if not posts:
            logger.info("LessWrong: no posts returned from API")
            return papers

        for post in posts:
            # Filter by karma client-side
            base_score = post.get("baseScore", 0) or 0
            if base_score < min_karma:
                continue

            title = (post.get("title") or "").strip()
            if not title:
                continue

            user_obj = post.get("user") or {}
            author = (user_obj.get("displayName") or "").strip()
            authors = [author] if author else ["Unknown"]

            abstract = _extract_abstract(post)
            url = _post_url(post)
            published_date = _parse_posted_date(post)

            papers.append(
                Paper(
                    title=title,
                    authors=authors,
                    organization="LessWrong",
                    abstract=abstract,
                    url=url,
                    published_date=published_date,
                    source_type="rss",
                    source_url=GRAPHQL_ENDPOINT,
                )
            )

        logger.info("LessWrong: %d posts collected", len(papers))

    except requests.RequestException as exc:
        logger.warning("Failed to fetch from LessWrong API", exc_info=True)
        status = "error"
        error = f"{type(exc).__name__}: {exc}"
    except (KeyError, ValueError) as exc:
        logger.warning(
            "Failed to parse LessWrong API response", exc_info=True
        )
        status = "error"
        error = f"{type(exc).__name__}: {exc}"

    if recorder is not None:
        recorder.record_source(
            name="LessWrong",
            org="LessWrong",
            type="lesswrong",
            items_fetched=len(papers),
            duration_seconds=time.perf_counter() - start,
            status=status,
            error=error,
        )

    return papers
