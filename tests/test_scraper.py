"""Unit tests for the web scraper fetcher."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

import requests

from scripts.fetchers.scraper import (
    _parse_date_string,
    _resolve_year_urls,
    fetch_scraped,
)


# ---------------------------------------------------------------------------
# {year} URL resolution
# ---------------------------------------------------------------------------

def test_resolve_year_urls_no_placeholder():
    assert _resolve_year_urls("https://example.com/blog") == [
        "https://example.com/blog"
    ]


def test_resolve_year_urls_current_year():
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    assert _resolve_year_urls("https://red.anthropic.com/{year}", now=now) == [
        "https://red.anthropic.com/2026"
    ]


def test_resolve_year_urls_january_includes_previous_year():
    now = datetime(2027, 1, 3, tzinfo=timezone.utc)
    assert _resolve_year_urls("https://red.anthropic.com/{year}", now=now) == [
        "https://red.anthropic.com/2027",
        "https://red.anthropic.com/2026",
    ]


def test_resolve_year_urls_december_stays_single():
    now = datetime(2026, 12, 31, tzinfo=timezone.utc)
    assert _resolve_year_urls("https://red.anthropic.com/{year}", now=now) == [
        "https://red.anthropic.com/2026"
    ]


# ---------------------------------------------------------------------------
# Date precision
# ---------------------------------------------------------------------------

def test_parse_date_string_full_date_is_day_precision():
    iso, precision = _parse_date_string("July 16, 2026")
    assert iso.startswith("2026-07-16")
    assert precision == "day"


def test_parse_date_string_month_only_is_month_precision():
    iso, precision = _parse_date_string("July 2026")
    assert iso.startswith("2026-07-")
    assert precision == "month"


# ---------------------------------------------------------------------------
# fetch_scraped
# ---------------------------------------------------------------------------

def _page(title_suffix: str, href: str) -> str:
    return f"""
<html><body>
<article>
  <h2>A Perfectly Plausible Research Paper {title_suffix}</h2>
  <span>July 15, 2026</span>
  <a href="{href}">Read</a>
  <p>An abstract about the paper.</p>
</article>
</body></html>
"""


def _response(html: str) -> mock.Mock:
    resp = mock.Mock(text=html)
    resp.raise_for_status = mock.Mock()
    return resp


def test_fetch_scraped_resolves_year_template():
    year = datetime.now(timezone.utc).year
    with mock.patch(
        "scripts.fetchers.scraper.requests.get",
        return_value=_response(_page("Alpha", "/posts/alpha/")),
    ) as mock_get:
        papers = fetch_scraped(
            [
                {
                    "name": "Red Team",
                    "url": "https://red.example.com/{year}",
                    "org": "Anthropic",
                }
            ]
        )

    requested = [call.args[0] for call in mock_get.call_args_list]
    assert f"https://red.example.com/{year}" in requested
    assert all("{year}" not in u for u in requested)
    assert papers, "expected at least one paper from the sample HTML"
    assert papers[0].url == "https://red.example.com/posts/alpha/"
    assert papers[0].source_url == f"https://red.example.com/{year}"


MONTH_ONLY_HTML = """
<html><body>
<article>
  <h2>A Month Dated Research Paper</h2>
  <time datetime="July 2026">July 2026</time>
  <a href="/posts/month-dated/">Read</a>
  <p>An abstract about the paper.</p>
</article>
</body></html>
"""


def test_fetch_scraped_sets_date_precision():
    with mock.patch(
        "scripts.fetchers.scraper.requests.get",
        return_value=_response(_page("Alpha", "/posts/alpha/")),
    ):
        day_papers = fetch_scraped(
            [{"name": "Plain", "url": "https://example.org/blog", "org": "Org"}]
        )
    with mock.patch(
        "scripts.fetchers.scraper.requests.get",
        return_value=_response(MONTH_ONLY_HTML),
    ):
        month_papers = fetch_scraped(
            [{"name": "Plain", "url": "https://example.org/blog", "org": "Org"}]
        )

    assert day_papers[0].date_precision == "day"
    assert month_papers[0].date_precision == "month"


def test_fetch_scraped_plain_url_unchanged():
    with mock.patch(
        "scripts.fetchers.scraper.requests.get",
        return_value=_response(_page("Alpha", "/posts/alpha/")),
    ) as mock_get:
        papers = fetch_scraped(
            [{"name": "Plain", "url": "https://example.org/blog", "org": "Org"}]
        )

    assert [call.args[0] for call in mock_get.call_args_list] == [
        "https://example.org/blog"
    ]
    assert papers[0].url == "https://example.org/posts/alpha/"
    assert papers[0].source_url == "https://example.org/blog"


def test_fetch_scraped_collects_papers_from_all_resolved_pages():
    pages = {
        "https://red.example.com/2027": _response(_page("Alpha", "/posts/alpha/")),
        "https://red.example.com/2026": _response(_page("Beta", "/posts/beta/")),
    }
    with mock.patch(
        "scripts.fetchers.scraper._resolve_year_urls",
        return_value=list(pages),
    ), mock.patch(
        "scripts.fetchers.scraper.requests.get",
        side_effect=lambda url, **kwargs: pages[url],
    ):
        papers = fetch_scraped(
            [
                {
                    "name": "Red Team",
                    "url": "https://red.example.com/{year}",
                    "org": "Anthropic",
                }
            ]
        )

    assert [(p.title, p.source_url) for p in papers] == [
        (
            "A Perfectly Plausible Research Paper Alpha",
            "https://red.example.com/2027",
        ),
        (
            "A Perfectly Plausible Research Paper Beta",
            "https://red.example.com/2026",
        ),
    ]


def test_fetch_scraped_skips_duplicate_links_across_pages():
    with mock.patch(
        "scripts.fetchers.scraper._resolve_year_urls",
        return_value=["https://red.example.com/2027", "https://red.example.com/2026"],
    ), mock.patch(
        "scripts.fetchers.scraper.requests.get",
        return_value=_response(_page("Alpha", "/posts/alpha/")),
    ):
        papers = fetch_scraped(
            [
                {
                    "name": "Red Team",
                    "url": "https://red.example.com/{year}",
                    "org": "Anthropic",
                }
            ]
        )

    assert len(papers) == 1
    assert papers[0].source_url == "https://red.example.com/2027"


def test_fetch_scraped_page_failure_does_not_abort_other_pages():
    def get(url, **kwargs):
        if url.endswith("/2027"):
            raise requests.ConnectionError("boom")
        return _response(_page("Beta", "/posts/beta/"))

    recorder = mock.Mock()
    with mock.patch(
        "scripts.fetchers.scraper._resolve_year_urls",
        return_value=["https://red.example.com/2027", "https://red.example.com/2026"],
    ), mock.patch("scripts.fetchers.scraper.requests.get", side_effect=get):
        papers = fetch_scraped(
            [
                {
                    "name": "Red Team",
                    "url": "https://red.example.com/{year}",
                    "org": "Anthropic",
                }
            ],
            recorder=recorder,
        )

    assert len(papers) == 1
    assert papers[0].source_url == "https://red.example.com/2026"
    kwargs = recorder.record_source.call_args.kwargs
    assert kwargs["status"] == "ok"
    assert "partial" in kwargs["error"]
    assert kwargs["items_fetched"] == 1


def test_fetch_scraped_all_pages_failing_marks_source_error():
    recorder = mock.Mock()
    with mock.patch(
        "scripts.fetchers.scraper.requests.get",
        side_effect=requests.ConnectionError("down"),
    ):
        papers = fetch_scraped(
            [{"name": "Plain", "url": "https://example.org/blog", "org": "Org"}],
            recorder=recorder,
        )

    assert papers == []
    kwargs = recorder.record_source.call_args.kwargs
    assert kwargs["status"] == "error"
    assert "ConnectionError" in kwargs["error"]
    assert kwargs["items_fetched"] == 0
