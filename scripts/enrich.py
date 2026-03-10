#!/usr/bin/env python3
"""
Enrich papers that have missing or short abstracts by fetching their URLs
and extracting a meaningful description from the page content.

This module exposes a single public function, ``enrich_abstracts``, which is
called by ``scripts/fetch.py`` after deduplication.

Extraction strategies (tried in order per URL type):
  - arXiv: abs/ page blockquote, then html/ variant, then meta descriptions
  - LessWrong: GraphQL API excerpt
  - Substack/blogs: meta descriptions, og:description, first substantial <p>
  - Generic pages: meta descriptions, semantic CSS classes, first <p> in
    article/main containers
  - Last resort: synthetic placeholder from metadata

Run standalone for testing:
    python scripts/enrich.py
"""

from __future__ import annotations

import html as html_module
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path so ``scripts.*`` imports work when
# the module is executed directly.
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.models import Paper

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MIN_ABSTRACT_LEN = 100      # abstracts shorter than this are considered "missing"
MIN_ABSTRACT_WORDS = 20     # abstracts with fewer words also trigger enrichment
MIN_PARAGRAPH_LEN = 80      # minimum length for a <p> tag to be considered useful
MAX_ABSTRACT_WORDS = 150    # cap enriched abstracts at this many words
MAX_CONCURRENT = 5           # polite concurrency limit
REQUEST_TIMEOUT = 10         # seconds per HTTP request
MAX_RETRIES = 1              # retry once on timeout / 5xx errors

# Rotate through realistic browser User-Agent strings so sites are less
# likely to block us as a bot.
USER_AGENTS = [
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
]

# LessWrong GraphQL endpoint
LESSWRONG_GRAPHQL_URL = "https://www.lesswrong.com/graphql"

# File extensions we should not attempt to parse as HTML
_PDF_EXTENSIONS = (".pdf", ".PDF")

# Boilerplate prefixes to strip from extracted abstracts
_BOILERPLATE_PREFIX_RE = re.compile(
    r"^(?:Abstract\s*[:.\-]\s*|Summary\s*[:.\-]\s*|TL;?\s*DR\s*[:.\-]\s*)",
    re.IGNORECASE,
)

# Trailing call-to-action phrases to strip
_CTA_SUFFIX_RE = re.compile(
    r"\s*(?:Read more\.?|Continue reading\.?|Click here\.?|Learn more\.?"
    r"|See more\.?|View full (?:article|paper|post)\.?)\.?\s*$",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP fetching with retry
# ---------------------------------------------------------------------------

def _get_user_agent(url: str) -> str:
    """Pick a User-Agent string deterministically based on the URL hash."""
    return USER_AGENTS[hash(url) % len(USER_AGENTS)]


def _is_pdf_url(url: str) -> bool:
    """Return True if the URL appears to point to a PDF file."""
    parsed = urlparse(url)
    return parsed.path.endswith(_PDF_EXTENSIONS)


def _is_retryable(exc: Exception) -> bool:
    """Return True if the exception indicates a retryable failure."""
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        resp = exc.response
        if resp is not None and resp.status_code >= 500:
            return True
    return False


def _fetch_url(url: str) -> requests.Response | None:
    """Fetch a URL with retry logic. Returns the Response or None on failure."""
    headers = {"User-Agent": _get_user_agent(url)}
    last_exc: Exception | None = None

    for attempt in range(1 + MAX_RETRIES):
        try:
            resp = requests.get(
                url,
                timeout=REQUEST_TIMEOUT,
                headers=headers,
                allow_redirects=True,
            )
            resp.raise_for_status()
            return resp
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_RETRIES and _is_retryable(exc):
                wait = 2 ** attempt
                logger.debug(
                    "Retrying %s in %ds after error: %s", url, wait, exc,
                )
                time.sleep(wait)
            else:
                break

    logger.debug("Failed to fetch %s after %d attempts: %s", url, 1 + MAX_RETRIES, last_exc)
    return None


# ---------------------------------------------------------------------------
# Text cleaning helpers
# ---------------------------------------------------------------------------

def _clean_text(text: str) -> str:
    """Collapse whitespace, unescape HTML entities, and strip."""
    # Unescape HTML entities like &amp; &lt; etc.
    text = html_module.unescape(text)
    # Remove any remaining HTML tags that might have leaked through
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _strip_boilerplate(text: str) -> str:
    """Remove common boilerplate prefixes and CTA suffixes."""
    text = _BOILERPLATE_PREFIX_RE.sub("", text).strip()
    text = _CTA_SUFFIX_RE.sub("", text).strip()
    return text


def _cap_words(text: str, max_words: int = MAX_ABSTRACT_WORDS) -> str:
    """Truncate text to at most *max_words* words, adding ellipsis if cut."""
    words = text.split()
    if len(words) <= max_words:
        return text
    truncated = " ".join(words[:max_words])
    # Try to end at a sentence boundary within the last portion
    last_period = truncated.rfind(". ")
    if last_period > len(truncated) * 0.6:
        return truncated[: last_period + 1]
    return truncated + "..."


def _finalize_abstract(text: str) -> str:
    """Clean, strip boilerplate, and cap an extracted abstract."""
    text = _clean_text(text)
    text = _strip_boilerplate(text)
    text = _cap_words(text)
    return text


# ---------------------------------------------------------------------------
# Extraction strategies
# ---------------------------------------------------------------------------

def _extract_meta_description(soup: BeautifulSoup) -> str | None:
    """Return the content of a meta description / og:description tag."""
    for attrs in (
        {"name": "description"},
        {"property": "og:description"},
        {"name": "twitter:description"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            cleaned = _clean_text(tag["content"])
            if len(cleaned) >= MIN_ABSTRACT_LEN:
                return cleaned
    return None


def _extract_arxiv_abstract(soup: BeautifulSoup, url: str) -> str | None:
    """For arXiv pages, pull the abstract from blockquote.abstract.

    Tries the standard abs/ page first, then falls back to the html/ variant.
    If the current soup doesn't contain the abstract and the URL can be
    converted to a different arXiv variant, fetches that variant.
    """
    if "arxiv.org" not in url:
        return None

    # Try extracting from the soup we already have
    block = soup.find("blockquote", class_="abstract")
    if block:
        descriptor = block.find("span", class_="descriptor")
        if descriptor:
            descriptor.decompose()
        cleaned = _clean_text(block.get_text())
        if len(cleaned) >= MIN_ABSTRACT_LEN:
            return cleaned

    # Also try a <div class="abstract"> or <p class="abstract"> (html/ pages)
    for tag_name in ("div", "p", "section"):
        block = soup.find(tag_name, class_="abstract")
        if block:
            cleaned = _clean_text(block.get_text())
            if len(cleaned) >= MIN_ABSTRACT_LEN:
                return cleaned

    # If we are on a pdf/ or html/ URL, try fetching the abs/ page instead
    alt_url = _arxiv_alt_url(url)
    if alt_url:
        logger.debug("Trying arXiv alternate URL: %s", alt_url)
        resp = _fetch_url(alt_url)
        if resp:
            try:
                alt_soup = BeautifulSoup(resp.text, "html.parser")
                block = alt_soup.find("blockquote", class_="abstract")
                if block:
                    descriptor = block.find("span", class_="descriptor")
                    if descriptor:
                        descriptor.decompose()
                    cleaned = _clean_text(block.get_text())
                    if len(cleaned) >= MIN_ABSTRACT_LEN:
                        return cleaned
            except Exception:
                pass

    return None


def _arxiv_alt_url(url: str) -> str | None:
    """Given an arXiv URL, return an alternate variant (abs/) if possible."""
    # Match patterns like arxiv.org/pdf/2301.12345 or arxiv.org/html/2301.12345
    m = re.search(r"arxiv\.org/(?:pdf|html)/(\d+\.\d+(?:v\d+)?)", url)
    if m:
        return f"https://arxiv.org/abs/{m.group(1)}"
    # If already an abs/ URL, try html/
    m = re.search(r"arxiv\.org/abs/(\d+\.\d+(?:v\d+)?)", url)
    if m:
        return f"https://arxiv.org/html/{m.group(1)}"
    return None


def _extract_from_lesswrong_api(url: str) -> str | None:
    """For LessWrong posts, use the GraphQL API to fetch the excerpt.

    The URL is expected to match ``lesswrong.com/posts/<postId>/...``.
    """
    if "lesswrong.com/posts/" not in url:
        return None

    # Extract the post ID from the URL
    m = re.search(r"lesswrong\.com/posts/([A-Za-z0-9]+)", url)
    if not m:
        return None
    post_id = m.group(1)

    query = {
        "query": """
            query PostExcerpt($input: SinglePostInput!) {
                post(input: $input) {
                    result {
                        excerpt
                        contents {
                            plaintextDescription
                        }
                    }
                }
            }
        """,
        "variables": {
            "input": {
                "selector": {
                    "_id": post_id,
                }
            }
        },
    }

    try:
        resp = requests.post(
            LESSWRONG_GRAPHQL_URL,
            json=query,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": _get_user_agent(url)},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug("LessWrong GraphQL failed for %s: %s", url, exc)
        return None

    try:
        result = data["data"]["post"]["result"]
        # Prefer the excerpt field
        excerpt = result.get("excerpt") or ""
        excerpt = _clean_text(excerpt)
        if len(excerpt) >= MIN_ABSTRACT_LEN:
            return excerpt
        # Fall back to plaintextDescription from contents
        contents = result.get("contents") or {}
        desc = contents.get("plaintextDescription") or ""
        desc = _clean_text(desc)
        if len(desc) >= MIN_ABSTRACT_LEN:
            return desc
    except (KeyError, TypeError):
        pass

    return None


def _extract_substack_blog(soup: BeautifulSoup, url: str) -> str | None:
    """For Substack and blog posts, try meta descriptions then first <p>.

    Substack pages reliably set ``<meta name="description">`` and
    ``og:description``.
    """
    host = urlparse(url).hostname or ""
    is_substack = "substack.com" in host or soup.find("meta", {"property": "article:publisher", "content": re.compile(r"substack", re.IGNORECASE)})

    if not is_substack:
        return None

    # For Substack, meta description is usually the best bet
    result = _extract_meta_description(soup)
    if result:
        return result

    # Fall back to first substantial paragraph
    result = _extract_first_paragraph(soup)
    if result:
        return result

    return None


def _extract_semantic_classes(soup: BeautifulSoup) -> str | None:
    """Look for elements with common abstract/summary CSS classes."""
    for class_name in ("abstract", "summary", "description", "post-excerpt",
                       "entry-summary", "article-summary", "paper-abstract"):
        # Search across common element types
        for tag_name in ("div", "p", "section", "span", "blockquote"):
            el = soup.find(tag_name, class_=class_name)
            if el:
                cleaned = _clean_text(el.get_text())
                if len(cleaned) >= MIN_ABSTRACT_LEN:
                    return cleaned
    return None


def _extract_first_paragraph(soup: BeautifulSoup) -> str | None:
    """Return the first substantial <p> inside article/main content areas."""
    containers = []

    for selector in ("article", "main", "[role='main']"):
        found = soup.select(selector)
        if found:
            containers.extend(found)

    # Fallback: search the whole body
    body = soup.find("body")
    if body:
        containers.append(body)

    for container in containers:
        for p in container.find_all("p"):
            text = _clean_text(p.get_text())
            if len(text) >= MIN_PARAGRAPH_LEN:
                return text
    return None


def _generate_synthetic_abstract(paper: Paper) -> str:
    """Generate a placeholder abstract from available metadata.

    This is used as a last resort when no abstract can be extracted from the
    URL. A synthetic abstract is better than an empty one for display purposes.
    """
    parts = []

    if paper.organization:
        parts.append(f"Research from {paper.organization}")
    else:
        parts.append("Research")

    if paper.title:
        parts.append(f"titled '{paper.title}'")

    sentence = " ".join(parts) + "."

    extras = []
    if paper.published_date:
        # Show just the date portion (YYYY-MM-DD) if it's an ISO string
        date_display = paper.published_date[:10]
        extras.append(f"Published {date_display}.")

    if paper.authors:
        author_str = ", ".join(paper.authors[:3])
        if len(paper.authors) > 3:
            author_str += f" and {len(paper.authors) - 3} others"
        extras.append(f"Authors: {author_str}.")

    return " ".join([sentence] + extras)


# ---------------------------------------------------------------------------
# Per-paper enrichment
# ---------------------------------------------------------------------------

def _fetch_abstract_from_url(url: str) -> str | None:
    """Fetch a URL and attempt to extract a useful abstract from the page.

    Returns ``None`` if nothing suitable is found or the request fails.
    Uses source-specific strategies ordered by reliability.
    """
    # Skip PDF URLs -- we cannot parse them as HTML
    if _is_pdf_url(url):
        logger.debug("Skipping PDF URL: %s", url)
        return None

    # --- Strategy 0: LessWrong GraphQL API (no HTML fetch needed) ---
    if "lesswrong.com/posts/" in url:
        result = _extract_from_lesswrong_api(url)
        if result:
            return _finalize_abstract(result)

    # --- Fetch the page HTML ---
    resp = _fetch_url(url)
    if resp is None:
        return None

    # Double-check Content-Type -- bail out if we got a PDF or binary response
    content_type = resp.headers.get("Content-Type", "")
    if "pdf" in content_type.lower() or "octet-stream" in content_type.lower():
        logger.debug("Skipping non-HTML content type for %s: %s", url, content_type)
        return None

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        logger.debug("Failed to parse HTML from %s: %s", url, exc)
        return None

    # --- Strategy 1: arXiv-specific extraction ---
    if "arxiv.org" in url:
        result = _extract_arxiv_abstract(soup, url)
        if result:
            return _finalize_abstract(result)
        # Fall through to generic strategies for arXiv

    # --- Strategy 2: Substack / blog-specific extraction ---
    result = _extract_substack_blog(soup, url)
    if result:
        return _finalize_abstract(result)

    # --- Strategy 3: meta description tags ---
    result = _extract_meta_description(soup)
    if result:
        return _finalize_abstract(result)

    # --- Strategy 4: semantic CSS classes (.abstract, .summary, etc.) ---
    result = _extract_semantic_classes(soup)
    if result:
        return _finalize_abstract(result)

    # --- Strategy 5: first substantial <p> in article/main ---
    result = _extract_first_paragraph(soup)
    if result:
        return _finalize_abstract(result)

    return None


def _enrich_single(paper: Paper) -> bool:
    """Try to enrich a single paper's abstract.

    Returns True if the abstract was updated, False otherwise.
    """
    new_abstract = _fetch_abstract_from_url(paper.url)
    if new_abstract:
        paper.abstract = new_abstract
        return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enrich_abstracts(papers: list[Paper]) -> list[Paper]:
    """Enrich papers that have missing or short abstracts.

    Papers whose abstract is already >= ``MIN_ABSTRACT_LEN`` characters are
    left untouched.  The function is idempotent -- running it again on
    already-enriched papers is a no-op.

    After URL-based enrichment, any papers that still lack an abstract
    receive a synthetic placeholder generated from their metadata.

    Parameters
    ----------
    papers:
        List of :class:`Paper` objects (mutated in place *and* returned).

    Returns
    -------
    list[Paper]
        The same list, with abstracts enriched where possible.
    """
    to_enrich = [
        p for p in papers
        if len((p.abstract or "").strip()) < MIN_ABSTRACT_LEN
        or len((p.abstract or "").split()) < MIN_ABSTRACT_WORDS
    ]

    if not to_enrich:
        logger.info("All %d papers already have good abstracts; nothing to enrich", len(papers))
        return papers

    logger.info(
        "Enriching abstracts: %d of %d papers need enrichment",
        len(to_enrich),
        len(papers),
    )

    enriched_count = 0
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as pool:
        future_to_paper = {
            pool.submit(_fetch_abstract_from_url, paper.url): paper
            for paper in to_enrich
        }
        for future in as_completed(future_to_paper):
            paper = future_to_paper[future]
            try:
                new_abstract = future.result()
            except Exception as exc:
                logger.warning("Enrichment failed for %s: %s", paper.title[:80], exc)
                continue

            if new_abstract:
                paper.abstract = _cap_words(new_abstract)
                enriched_count += 1
                logger.info("  Enriched: %s", paper.title[:80])
            else:
                logger.debug("  No URL enrichment for: %s", paper.title[:80])

    # ---- Synthetic fallback for anything still missing ----
    synthetic_count = 0
    for paper in papers:
        abstract_text = (paper.abstract or "").strip()
        if len(abstract_text) < MIN_ABSTRACT_LEN or len(abstract_text.split()) < MIN_ABSTRACT_WORDS:
            paper.abstract = _generate_synthetic_abstract(paper)
            synthetic_count += 1
            logger.debug("  Synthetic abstract for: %s", paper.title[:80])

    logger.info(
        "Enrichment complete: %d/%d papers enriched from URLs, "
        "%d received synthetic abstracts",
        enriched_count,
        len(to_enrich),
        synthetic_count,
    )
    return papers


# ---------------------------------------------------------------------------
# Standalone mode -- enrich an existing data/papers.json on disk
# ---------------------------------------------------------------------------

def main() -> None:
    """Load papers.json, enrich, and write back."""
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    papers_path = os.path.join(PROJECT_ROOT, "data", "papers.json")
    if not os.path.exists(papers_path):
        logger.error("No papers.json found at %s", papers_path)
        sys.exit(1)

    with open(papers_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    papers = [Paper.from_dict(d) for d in raw]
    logger.info("Loaded %d papers from %s", len(papers), papers_path)

    papers = enrich_abstracts(papers)

    papers_dicts = [p.to_dict() for p in papers]
    with open(papers_path, "w", encoding="utf-8") as f:
        json.dump(papers_dicts, f, indent=2, ensure_ascii=False)

    logger.info("Wrote %d papers back to %s", len(papers), papers_path)


if __name__ == "__main__":
    main()
