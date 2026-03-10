"""Fetch AI-related tweets from configured Twitter/X accounts.

Requires the ``TWITTER_BEARER_TOKEN`` environment variable to be set with an
X API v2 Bearer Token (Basic tier or above).  If the token is absent the
fetcher logs a warning and returns an empty list so the rest of the pipeline
continues normally.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

from scripts.models import Paper

logger = logging.getLogger(__name__)

# Max characters from the tweet to use as the Paper title
_TITLE_MAX_CHARS = 120


def _truncate_title(text: str) -> str:
    """Build a short title from the first sentence or ~120 chars of a tweet."""
    # Remove URLs for a cleaner title
    clean = re.sub(r"https?://\S+", "", text).strip()
    if not clean:
        return text[:_TITLE_MAX_CHARS]
    # Try to cut at the first sentence boundary
    match = re.search(r"[.!?]\s", clean)
    if match and match.end() <= _TITLE_MAX_CHARS:
        return clean[: match.end()].strip()
    if len(clean) <= _TITLE_MAX_CHARS:
        return clean
    # Cut at last word boundary before limit
    truncated = clean[:_TITLE_MAX_CHARS].rsplit(" ", 1)[0]
    return truncated + "\u2026"


def _matches_keywords(text: str, keywords: list[str]) -> bool:
    """Return True if *text* contains at least one keyword (case-insensitive)."""
    lowered = text.lower()
    return any(kw.lower() in lowered for kw in keywords)


def fetch_twitter(config: dict) -> list[Paper]:
    """Fetch recent AI-related tweets from configured accounts.

    Parameters
    ----------
    config : dict
        The ``twitter`` section from config.yaml.  Expected keys:
        ``accounts`` (list of {username, org}), ``keywords`` (list[str]),
        ``max_results_per_user`` (int), ``days_back`` (int).

    Returns
    -------
    list[Paper]
    """
    bearer_token = os.environ.get("TWITTER_BEARER_TOKEN", "")
    if not bearer_token:
        logger.info(
            "TWITTER_BEARER_TOKEN not set — skipping Twitter/X fetcher"
        )
        return []

    # Late import so the rest of the pipeline works even if tweepy isn't installed
    try:
        import tweepy  # type: ignore[import-untyped]
    except ImportError:
        logger.warning(
            "tweepy is not installed — skipping Twitter/X fetcher. "
            "Install with: pip3 install tweepy"
        )
        return []

    accounts = config.get("accounts", [])
    keywords = config.get("keywords", [])
    max_results = config.get("max_results_per_user", 50)
    days_back = config.get("days_back", 7)

    if not accounts:
        logger.info("No Twitter accounts configured — skipping")
        return []

    client = tweepy.Client(bearer_token=bearer_token, wait_on_rate_limit=True)

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    papers: list[Paper] = []

    for account_cfg in accounts:
        username = account_cfg.get("username", "")
        org = account_cfg.get("org", username)

        if not username:
            continue

        logger.info("Twitter: fetching tweets from @%s", username)

        try:
            # Look up the user ID from the username
            user_resp = client.get_user(username=username)
            if not user_resp or not user_resp.data:
                logger.warning("Twitter: user @%s not found", username)
                continue
            user_id = user_resp.data.id

            # Fetch recent tweets (excludes retweets and replies by default)
            tweets_resp = client.get_users_tweets(
                id=user_id,
                max_results=min(max_results, 100),  # API max per page is 100
                tweet_fields=["created_at", "text", "author_id"],
                exclude=["retweets", "replies"],
                start_time=cutoff.isoformat(),
            )

            if not tweets_resp or not tweets_resp.data:
                logger.info("Twitter: no recent tweets from @%s", username)
                continue

            account_count = 0
            for tweet in tweets_resp.data:
                text = tweet.text or ""

                # Apply keyword filter
                if keywords and not _matches_keywords(text, keywords):
                    continue

                title = _truncate_title(text)
                if len(title) < 10:
                    continue

                tweet_url = f"https://x.com/{username}/status/{tweet.id}"
                pub_date = tweet.created_at
                if pub_date and pub_date.tzinfo is None:
                    pub_date = pub_date.replace(tzinfo=timezone.utc)

                papers.append(
                    Paper(
                        title=title,
                        authors=[f"@{username}"],
                        organization=org,
                        abstract=text,
                        url=tweet_url,
                        published_date=pub_date.isoformat() if pub_date else "",
                        source_type="twitter",
                        source_url=f"https://x.com/{username}",
                    )
                )
                account_count += 1

            logger.info(
                "Twitter @%s: %d tweets matched keywords out of %d",
                username,
                account_count,
                len(tweets_resp.data),
            )

        except Exception:
            logger.warning(
                "Twitter: failed to fetch @%s", username, exc_info=True
            )
            continue

    logger.info("Twitter total: %d tweets collected", len(papers))
    return papers
