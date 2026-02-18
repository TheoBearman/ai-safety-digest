# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

AI safety **research** digest with daily/weekly toggle. Python scripts fetch from multiple sources, deduplicate, enrich abstracts, filter for research relevance, and render a static HTML site. Deployed via GitHub Pages with a daily 9AM UTC cron workflow.

## Commands

```bash
# Install dependencies
pip3 install -r requirements.txt

# Fetch papers from all sources → data/papers.json
python3 scripts/fetch.py

# Render static site → site/index.html
python3 scripts/render.py

# Full local update + open in browser
bash scripts/update-and-open.sh
```

There are no tests or linting configured.

## Architecture

**Pipeline:** `config.yaml` → fetchers → **7-day date filter** → dedup → research filter → enrich → clean → `data/papers.json` → `render.py` → `site/index.html`

**Data model:** Everything flows through `scripts/models.Paper` dataclass. Fields: title, authors, organization, abstract, url, published_date, source_type (`"rss"`, `"scrape"`), source_url, fetched_at. All fetchers must return `list[Paper]`.

**Fetchers** (`scripts/fetchers/`):
- `rss.py` — RSS/Atom feeds via feedparser. 7-day window. Three-layer filtering: RSS `categories` tags, explicit per-feed `keywords`, default research keywords.
- `scraper.py` — BeautifulSoup scraper for orgs without RSS. Heuristic article element detection (articles → class-matched elements → heading links → container links). Optional `link_must_contain` filter. Papers without parseable dates are dropped. Supports "Month Year" date format.
- `lesswrong.py` — LessWrong GraphQL API. Filters by karma threshold (150+) client-side.
- `trending.py` — HN (Algolia API) + Reddit JSON API. Research content filtering via URL domain checks and title keyword analysis. All use `source_type="rss"`.

**Processing** (called by `fetch.py` in order):
1. **Global 7-day date filter** — Removes all papers older than 7 days. Applied to ALL sources before dedup.
2. `dedup.py` — Two-pass: exact normalized title match, then SequenceMatcher (ratio > 0.85). Keeps entry with longest abstract.
3. **Research relevance filter** (`filter.py`) — Scoring-based: 144 research terms checked against title+abstract. Known research orgs need score >= 1, others >= 2.
3. `enrich.py` — Fetches URLs of papers with short/missing abstracts (<50 chars). Strategies: LessWrong GraphQL API, arXiv abs/html pages, meta descriptions, semantic CSS classes, first paragraph. Synthetic fallback for remaining. ThreadPoolExecutor (5 workers), retry on 5xx/timeout, User-Agent rotation.
4. Abstract cleaning in `fetch.py` — strips HTML, collapses whitespace, removes date prefixes, caps at 150 words.

**Rendering** (`render.py`):
- Jinja2 template at `templates/index.html.j2`, CSS inlined from `static/style.css`.
- Featured section: up to 3 papers selected by multi-signal scoring (source authority tiers, abstract richness, research title terms, named authors, exponential recency decay). Org diversity enforced. Minimum score threshold (12.0).
- Client-side JS org filter and daily/weekly digest toggle. Dark mode support.
- Daily/weekly toggle is purely client-side: filters paper cards by `data-date` attribute, updates header text and counts, persists choice in `localStorage`. Backend always fetches 7 days.

**Deployment:** GitHub Pages serves `site/` directory. Daily cron workflow (9 AM UTC) fetches, renders, commits, and deploys.

## Adding a New Source

- **RSS/Atom feed:** Add entry to `rss_feeds` in `config.yaml`. Use `keywords` for keyword filtering, or `categories` for RSS `<category>` tag filtering.
- **Web scraper:** Add entry to `scrapers` in `config.yaml`. Use `link_must_contain` to filter noise. Page must have parseable dates or papers will be dropped.
- **New fetcher type:** Create `scripts/fetchers/new_fetcher.py` returning `list[Paper]`, wire it into `fetch.py` main loop, add config section to `config.yaml`.

## Conventions

- All Python files use `from __future__ import annotations` (Python 3.9 compatibility).
- Use `pip3` not `pip` on the dev machine.
- Scripts are run from the project root. Each script adds `PROJECT_ROOT` to `sys.path` so `scripts.*` imports work when invoked directly.
- Output artifacts: `data/papers.json` (committed), `site/index.html` (committed, deployed to GitHub Pages).
