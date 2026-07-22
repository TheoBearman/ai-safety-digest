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

# Run snapshot tests
python3 -m pytest

# Regenerate render snapshot after intentional template/CSS changes
UPDATE_SNAPSHOTS=1 python3 -m pytest
```

Tests live in `tests/` (pytest). Snapshot output is at `tests/snapshots/index.html`. No linter is configured.

## Architecture

**Pipeline:** `config.yaml` → fetchers → **7-day date filter** → dedup → research filter → enrich → clean → `data/papers.json` → `render.py` → `site/index.html`

**Data model:** Everything flows through `scripts/models.Paper` dataclass. Fields: title, authors, organization, abstract, url, published_date, source_type (`"rss"`, `"scrape"`), source_url, fetched_at, date_precision (`"day"` | `"month"` — month-only scraped dates get a synthesized day placeholder). All fetchers must return `list[Paper]`.

**Fetchers** (`scripts/fetchers/`):
- `rss.py` — RSS/Atom feeds via feedparser. 7-day window. Three-layer filtering: RSS `categories` tags, explicit per-feed `keywords`, default research keywords.
- `scraper.py` — BeautifulSoup scraper for orgs without RSS. Heuristic article element detection (articles → class-matched elements → heading links → container links). Optional `link_must_contain` filter. Papers without parseable dates are dropped. Supports "Month Year" date format — such papers get a synthesized day (today within the current month, else the 15th) and are marked `date_precision="month"` so enrichment can refine them and the renderer never displays the fake day. URLs may contain a `{year}` placeholder resolved to the current UTC year at fetch time (plus the previous year during January, so the 7-day window spanning New Year is covered).
- `lesswrong.py` — LessWrong GraphQL API. Filters by karma threshold (150+) client-side.
- `trending.py` — HN (Algolia API) + Reddit JSON API. Research content filtering via URL domain checks and title keyword analysis. All use `source_type="rss"`.

**Processing** (called by `fetch.py` in order):
1. **Global 7-day date filter** — Removes all papers older than 7 days. Applied to ALL sources before dedup.
2. `dedup.py` — Two-pass: exact normalized title match, then SequenceMatcher (ratio > 0.85). Keeps entry with longest abstract.
3. **Research relevance filter** (`filter.py`) — Scoring-based: 144 research terms checked against title+abstract. Known research orgs need score >= 1, others >= 2.
3. `enrich.py` — Fetches URLs of papers with short/missing abstracts (<50 chars). Strategies: LessWrong GraphQL API, arXiv abs/html pages, meta descriptions, semantic CSS classes, first paragraph. Synthetic fallback for remaining. ThreadPoolExecutor (5 workers), retry on 5xx/timeout, User-Agent rotation. Also refines `date_precision="month"` dates by fetching the article page and reading `article:published_time` meta tags, JSON-LD `datePublished`, or (only when the year-month matches the scraped listing date) `<time>` tags; results are cached in `enrich_cache.json` under date-specific keys. After enrichment, `fetch.py` re-applies the 7-day cutoff so papers whose refined date is older than the window drop out.
4. Abstract cleaning in `fetch.py` — strips HTML, collapses whitespace, removes date prefixes, caps at 150 words.

**Rendering** (`render.py`):
- Jinja2 template at `templates/index.html.j2`, CSS inlined from `static/style.css`. Papers with `date_precision="month"` display as "Jul 2026" instead of a fabricated exact day.
- Featured section: up to 3 papers selected by multi-signal scoring (source authority tiers, abstract richness, research title terms, named authors, exponential recency decay). Org diversity enforced. Minimum score threshold (12.0).
- Client-side JS org filter and daily/weekly digest toggle. Dark mode support.
- Daily/weekly toggle is purely client-side: filters paper cards by `data-date` attribute, updates header text and counts, persists choice in `localStorage`. Backend always fetches 7 days.

**Deployment:** GitHub Pages serves `site/` directory. Daily cron workflow (9 AM UTC) fetches, renders, commits, and deploys.

**Observability artifacts** (managed by `scripts/observability.py` and `scripts/enrich.py`):
- `data/run_log.jsonl` — append-only run log. One JSON line per pipeline invocation containing per-source metrics (name, org, items_fetched, duration, status, error), per-stage metrics (date_filter, dedup, research_filter, enrich), and `enrich_cache` hit/miss stats. Committed.
- `data/enrich_cache.json` — URL → cached abstract map for `enrich.py`. Successful entries persist indefinitely; failures expire after 7 days; entries older than 180 days are evicted on save. Committed.
- `compute_health()` in `observability.py` reads the run log and classifies sources as `healthy` / `degraded` / `broken` / `unknown`. The renderer surfaces this as a warning banner (when any source is broken) and a collapsible footer section on the public site, so silent fetcher failures are visible.

**Adding observability to a new fetcher:** accept `recorder: RunRecorder | None = None`, time each source/site, and call `recorder.record_source(name, org, type, items_fetched, duration_seconds, status, error)` once per source — even on failure (use a `try/finally`). Pipeline stages should accept the same kwarg and call `recorder.record_stage(name, in_count, out_count, duration_seconds)`.

## Adding a New Source

- **RSS/Atom feed:** Add entry to `rss_feeds` in `config.yaml`. Use `keywords` for keyword filtering, or `categories` for RSS `<category>` tag filtering.
- **Web scraper:** Add entry to `scrapers` in `config.yaml`. Use `link_must_contain` to filter noise. Page must have parseable dates or papers will be dropped. For year-scoped archive URLs, use a `{year}` placeholder (e.g. `https://red.anthropic.com/{year}`).
- **New fetcher type:** Create `scripts/fetchers/new_fetcher.py` returning `list[Paper]`, wire it into `fetch.py` main loop, add config section to `config.yaml`.

## Conventions

- All Python files use `from __future__ import annotations` (Python 3.9 compatibility).
- Use `pip3` not `pip` on the dev machine.
- Scripts are run from the project root. Each script adds `PROJECT_ROOT` to `sys.path` so `scripts.*` imports work when invoked directly.
- Output artifacts: `data/papers.json` (committed), `site/index.html` (committed, deployed to GitHub Pages).
