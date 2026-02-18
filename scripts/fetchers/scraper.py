"""Scrape research pages for papers and articles using requests + BeautifulSoup."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from scripts.models import Paper

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; AISafetyDigestBot/1.0; "
        "+https://github.com/ai-safety-digest)"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

REQUEST_TIMEOUT = 15  # seconds

# Regex for dates like "2026-01-15", "Jan 15, 2026", "15 January 2026", etc.
DATE_PATTERNS = [
    # ISO-style
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    # "January 15, 2026" or "January 15 2026"
    re.compile(
        r"\b((?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2},?\s+\d{4})\b",
        re.IGNORECASE,
    ),
    # "Jan 15, 2026"
    re.compile(
        r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"\s+\d{1,2},?\s+\d{4})\b",
        re.IGNORECASE,
    ),
    # "15 January 2026"
    re.compile(
        r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|"
        r"August|September|October|November|December)\s+\d{4})\b",
        re.IGNORECASE,
    ),
    # "February 2026" (month + year only, no day)
    re.compile(
        r"\b((?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{4})\b",
        re.IGNORECASE,
    ),
]

DATE_FORMATS = [
    "%Y-%m-%d",
    "%B %d, %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%B %Y",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date_string(text: str) -> Optional[str]:
    """
    Attempt to extract and parse a date from an arbitrary string.

    Returns an ISO-formatted date string on success, or None.
    """
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            date_str = match.group(1)
            for fmt in DATE_FORMATS:
                try:
                    dt = datetime.strptime(date_str, fmt).replace(
                        tzinfo=timezone.utc
                    )
                    # For month-only dates (e.g. "February 2026"), use the 15th
                    # so current-month papers aren't excluded by the 7-day filter.
                    if fmt == "%B %Y":
                        dt = dt.replace(day=15)
                    return dt.isoformat()
                except ValueError:
                    continue
    return None


def _extract_date(element) -> Optional[str]:
    """
    Try to find a date associated with a DOM element.

    Checks <time> tags first, then searches visible text for date patterns.
    """
    # <time datetime="...">
    time_tag = element.find("time")
    if time_tag:
        dt_attr = time_tag.get("datetime", "")
        if dt_attr:
            parsed = _parse_date_string(dt_attr)
            if parsed:
                return parsed
        # Try the text content of <time>
        parsed = _parse_date_string(time_tag.get_text(strip=True))
        if parsed:
            return parsed

    # Fall back to scanning the element text
    text = element.get_text(" ", strip=True)
    parsed = _parse_date_string(text)
    if parsed:
        return parsed

    # Check previous siblings for date (common pattern: date div before article links)
    for sibling in element.previous_siblings:
        if hasattr(sibling, "get_text"):
            sib_text = sibling.get_text(strip=True)
            if sib_text:
                parsed = _parse_date_string(sib_text)
                if parsed:
                    return parsed
                # Stop after checking a few siblings to avoid scanning the whole page
                break

    return None


def _extract_title(element) -> str:
    """Extract a title from headings or the first prominent link."""
    # Search both the element and its first link for headings
    candidates = [element]
    first_link = element.find("a")
    if first_link:
        candidates.append(first_link)

    for candidate in candidates:
        for tag in ("h1", "h2", "h3", "h4"):
            heading = candidate.find(tag)
            if heading:
                return heading.get_text(strip=True)

    # Fallback: use link text, but get_text with separator to avoid
    # concatenation of child elements (title + date + desc + "Read more")
    if first_link:
        text = first_link.get_text(" ", strip=True)
        if text and len(text) <= 200:
            return text

    return ""


def _extract_abstract(element) -> str:
    """Extract a description from <p> tags within the element."""
    paragraphs = element.find_all("p")
    texts = [p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True)]
    return " ".join(texts) if texts else ""


def _extract_link(element, base_url: str) -> str:
    """Extract the most relevant href from an element."""
    # If the element itself is an <a> tag, use its href directly
    if element.name == "a" and element.get("href"):
        return urljoin(base_url, element["href"])
    link = element.find("a", href=True)
    if link:
        href = link["href"]
        return urljoin(base_url, href)
    return base_url


ARTICLE_CLASSES = re.compile(
    r"post|card|entry|research|publication|article|blog|item", re.IGNORECASE
)

# Titles that are obviously not papers/articles
_JUNK_TITLES = {
    "home", "about", "about us", "careers", "donate", "contact", "team",
    "privacy", "privacy policy", "transparency", "news", "blog", "research",
    "events", "programs", "faq", "alumni", "save & accept", "necessary",
    "privacy overview", "policies", "work", "publications",
}

# Minimum title length to avoid single-word nav items
_MIN_TITLE_LENGTH = 10


def _is_junk_title(title: str) -> bool:
    """Return True if the title looks like navigation / non-paper content."""
    stripped = title.strip().rstrip(".")
    if stripped.lower() in _JUNK_TITLES:
        return True
    if len(stripped) < _MIN_TITLE_LENGTH:
        return True
    # Reject titles that are just domain names or URLs
    if stripped.startswith("http") or ".gov" in stripped or ".com" in stripped:
        return True
    return False


def _find_article_elements(soup: BeautifulSoup) -> list:
    """
    Locate article-like DOM elements using multiple heuristics.

    Returns a list of BeautifulSoup Tag objects.
    """
    # 1. Explicit <article> tags
    articles = soup.find_all("article")
    if articles:
        return articles

    # 2. Divs / sections / list-items whose class matches typical patterns
    candidates = soup.find_all(
        ["div", "section", "li", "a"],
        class_=ARTICLE_CLASSES,
    )
    if candidates:
        return candidates

    # 3. <a> tags that contain headings — common pattern for article cards
    heading_links = [
        a for a in soup.find_all("a", href=True)
        if a.find(["h2", "h3", "h4"])
    ]
    if heading_links:
        return heading_links

    # 4. Links inside known container ids / classes
    for container_attr in ("main", "content", "posts", "articles", "blog"):
        container = soup.find(id=re.compile(container_attr, re.IGNORECASE))
        if container is None:
            container = soup.find(
                class_=re.compile(container_attr, re.IGNORECASE)
            )
        if container:
            links = container.find_all("a", href=True)
            if links:
                return links

    return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_scraped(scrapers_config: list[dict]) -> list[Paper]:
    """
    Scrape a list of research pages for articles.

    Parameters
    ----------
    scrapers_config : list[dict]
        Each dict must contain ``url`` and ``org``, and optionally ``name``.
        Optional ``link_must_contain`` restricts results to links whose URL
        contains the given substring.

    Returns
    -------
    list[Paper]
    """
    papers: list[Paper] = []

    for site_cfg in scrapers_config:
        site_url = site_cfg.get("url", "")
        org = site_cfg.get("org", "Unknown")
        site_name = site_cfg.get("name", site_url)
        link_must_contain = site_cfg.get("link_must_contain", "")

        logger.info("Scraping: %s (%s)", site_name, site_url)

        try:
            response = requests.get(
                site_url, headers=HEADERS, timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")
            elements = _find_article_elements(soup)

            if not elements:
                logger.info("No article elements found on %s", site_name)
                continue

            site_count = 0
            for elem in elements:
                title = _extract_title(elem)
                if not title:
                    continue

                # Filter obvious non-paper content
                if _is_junk_title(title):
                    continue

                # Truncate garbled titles (e.g. card text with date/desc concatenated)
                if len(title) > 200:
                    title = title[:200].rsplit(" ", 1)[0] + "..."

                link = _extract_link(elem, site_url)

                # Apply URL filter if configured
                if link_must_contain and link_must_contain not in link:
                    continue

                # Skip links that point back to the exact page we're scraping
                if link.rstrip("/") == site_url.rstrip("/"):
                    continue

                abstract = _extract_abstract(elem)
                pub_date = _extract_date(elem)

                # Skip papers without a parseable date — they are likely
                # old papers from a research listing page.
                if pub_date is None:
                    continue

                papers.append(
                    Paper(
                        title=title,
                        authors=[],
                        organization=org,
                        abstract=abstract,
                        url=link,
                        published_date=pub_date,
                        source_type="scrape",
                        source_url=site_url,
                    )
                )
                site_count += 1

            logger.info(
                "Scraper %s: found %d items from %d elements",
                site_name,
                site_count,
                len(elements),
            )

        except Exception:
            logger.warning("Failed to scrape %s", site_name, exc_info=True)
            continue

    logger.info("Scraper total: %d papers collected", len(papers))
    return papers
