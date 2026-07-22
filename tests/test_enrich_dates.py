"""Tests for month-precision date refinement in scripts.enrich."""

from __future__ import annotations

from unittest import mock

from bs4 import BeautifulSoup

from scripts.enrich import (
    _extract_published_date_from_page,
    _generate_synthetic_abstract,
    cache_store,
    enrich_abstracts,
)
from scripts.models import Paper

GOOD_ABSTRACT = (
    "This abstract is comfortably long enough that the enrichment pipeline "
    "will not try to replace it with anything fetched from the paper URL, "
    "because it exceeds both the character and word thresholds."
)

META_HTML = """
<html><head>
<meta property="article:published_time" content="2026-07-16T11:00:00+00:00">
</head><body><p>Body text.</p></body></html>
"""

JSON_LD_HTML = """
<html><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Article",
 "datePublished": "2026-07-16T11:00:00+00:00"}
</script>
</head><body><p>Body text.</p></body></html>
"""

TIME_TAG_HTML = """
<html><body>
<article><time datetime="2025-06-03">June 3, 2025</time></article>
</body></html>
"""

NO_DATE_HTML = "<html><body><p>Nothing datelike here.</p></body></html>"


def _paper(**overrides) -> Paper:
    base = dict(
        title="Our approach to bioresilience",
        authors=[],
        organization="Google DeepMind",
        abstract=GOOD_ABSTRACT,
        url="https://example.com/blog/bioresilience/",
        published_date="2026-07-22T00:00:00+00:00",
        source_type="scrape",
        source_url="https://example.com/blog",
        date_precision="month",
    )
    base.update(overrides)
    return Paper(**base)


def _response(html: str) -> mock.Mock:
    return mock.Mock(text=html, headers={"Content-Type": "text/html"})


def _run_enrich(papers, fetch_response, cache=None):
    """Run enrich_abstracts with the on-disk cache mocked out."""
    cache = {} if cache is None else cache
    with mock.patch(
        "scripts.enrich.load_enrich_cache", return_value=cache
    ), mock.patch(
        "scripts.enrich.save_enrich_cache", return_value=len(cache)
    ), mock.patch(
        "scripts.enrich._fetch_url", return_value=fetch_response
    ) as mock_fetch:
        result = enrich_abstracts(papers)
    return result, mock_fetch, cache


# ---------------------------------------------------------------------------
# Page date extraction
# ---------------------------------------------------------------------------

def test_extract_date_from_article_meta():
    soup = BeautifulSoup(META_HTML, "html.parser")
    assert (
        _extract_published_date_from_page(soup)
        == "2026-07-16T11:00:00+00:00"
    )


def test_extract_date_from_json_ld():
    soup = BeautifulSoup(JSON_LD_HTML, "html.parser")
    assert (
        _extract_published_date_from_page(soup)
        == "2026-07-16T11:00:00+00:00"
    )


def test_time_tag_requires_year_month_match():
    """<time> tags may belong to related-article cards, so they only count
    when they agree with the month scraped from the listing page."""
    soup = BeautifulSoup(TIME_TAG_HTML, "html.parser")
    assert _extract_published_date_from_page(soup, (2026, 7)) is None
    assert _extract_published_date_from_page(soup, (2025, 6)).startswith(
        "2025-06-03"
    )


# ---------------------------------------------------------------------------
# Refinement through enrich_abstracts
# ---------------------------------------------------------------------------

def test_enrich_refines_month_precision_date():
    paper = _paper()
    _run_enrich([paper], _response(META_HTML))
    assert paper.published_date == "2026-07-16T11:00:00+00:00"
    assert paper.date_precision == "day"


def test_enrich_refines_even_when_abstracts_need_no_work():
    """Refinement must not be skipped by the all-abstracts-good early return."""
    paper = _paper(abstract=GOOD_ABSTRACT)
    _run_enrich([paper], _response(META_HTML))
    assert paper.date_precision == "day"


def test_enrich_leaves_day_precision_papers_alone():
    paper = _paper(date_precision="day")
    _, mock_fetch, _ = _run_enrich([paper], _response(META_HTML))
    assert paper.published_date == "2026-07-22T00:00:00+00:00"
    mock_fetch.assert_not_called()


def test_failed_refinement_keeps_month_precision_and_caches():
    paper = _paper()
    _, mock_fetch, cache = _run_enrich([paper], _response(NO_DATE_HTML))
    assert paper.date_precision == "month"
    assert mock_fetch.call_count == 1

    # A second run with the same cache must not refetch the URL.
    paper2 = _paper()
    _, mock_fetch2, _ = _run_enrich([paper2], _response(NO_DATE_HTML), cache)
    assert paper2.date_precision == "month"
    mock_fetch2.assert_not_called()


def test_refined_date_survives_abstract_cache_store():
    """cache_store must merge into existing entries, not clobber date fields."""
    url = "https://example.com/blog/bioresilience/"
    cache: dict[str, dict] = {}
    paper = _paper()
    _run_enrich([paper], _response(META_HTML), cache)
    cache_store(cache, url, "A real abstract fetched later.")
    assert cache[url]["published_date"] == "2026-07-16T11:00:00+00:00"
    assert cache[url]["abstract"] == "A real abstract fetched later."


# ---------------------------------------------------------------------------
# Synthetic abstracts
# ---------------------------------------------------------------------------

def test_synthetic_abstract_shows_month_for_month_precision():
    paper = _paper(abstract="")
    text = _generate_synthetic_abstract(paper)
    assert "Published July 2026." in text
    assert "2026-07-22" not in text


def test_synthetic_abstract_shows_full_date_for_day_precision():
    paper = _paper(abstract="", date_precision="day")
    text = _generate_synthetic_abstract(paper)
    assert "Published 2026-07-22." in text
