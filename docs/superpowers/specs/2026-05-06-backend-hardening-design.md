# Backend Hardening Design — 2026-05-06

## Goals

Make the digest pipeline introspectable and resilient against silent failure. Four bundled sub-features, all sharing the same observability spine:

1. **Per-source health checks** — detect fetchers that have stopped returning data (the dominant failure mode for scrapers when sites get redesigned).
2. **Enrichment cache** — stop re-fetching URLs we've already enriched successfully.
3. **Structured logging + metrics** — record per-source counts, durations, and pipeline-stage stats per run for trend analysis.
4. **Snapshot tests** for `render.py` — catch regressions in template/CSS output.

## Non-goals

- No external observability stack (no Datadog, no Prometheus, no log shipping). Everything lives in `data/` and is served from the static site.
- No dashboard/alerting beyond what the rendered site shows.
- No retroactive backfill of historical metrics — health begins accruing on first run after deploy.

## Architecture

### New module: `scripts/observability.py`

Single source of truth for run-level state. Exposes:

- **`RunRecorder`** — created once per pipeline invocation in `fetch.py`. Methods:
  - `record_source(name, org, type, items_fetched, duration_seconds, status, error=None)` — called once per source (RSS feed, scraper target, LessWrong, etc.).
  - `record_stage(name, in_count, out_count, duration_seconds)` — called for `dedup`, `research_filter`, `enrich`, `date_filter`.
  - `record_enrich_cache(hits, misses, size_after)` — called once at end of enrichment.
  - `finalize() -> dict` — assembles the run entry and appends to `data/run_log.jsonl`.

- **`compute_health(window=7) -> dict[str, SourceHealth]`** — reads tail of `run_log.jsonl`, returns per-source health for the last `window` runs:
  - `healthy` — at least one successful run with `items_fetched > 0` in the last 3 runs.
  - `degraded` — last successful run was 3-7 runs ago.
  - `broken` — no successful run with `items_fetched > 0` in the last 7 runs.
  - `unknown` — fewer than 3 historical runs available (suppresses warnings during initial rollout).
  - Each entry also carries `last_success_at`, `last_run_at`, `last_items_fetched`, `last_error`.

### Run log format

`data/run_log.jsonl` — append-only JSON Lines, committed to the repo. Each line is one run:

```json
{
  "run_id": "2026-05-06T11:08:21Z",
  "started_at": "2026-05-06T11:08:00Z",
  "completed_at": "2026-05-06T11:09:30Z",
  "duration_seconds": 90.0,
  "sources": [
    {
      "name": "Anthropic Alignment Science",
      "org": "Anthropic",
      "type": "scrape",
      "items_fetched": 3,
      "duration_seconds": 2.4,
      "status": "ok",
      "error": null
    }
  ],
  "stages": {
    "date_filter":     {"in": 168, "out": 152, "duration_seconds": 0.01},
    "dedup":           {"in": 152, "out": 134, "duration_seconds": 0.4},
    "research_filter": {"in": 134, "out":  89, "duration_seconds": 0.05},
    "enrich":          {"in":  89, "out":  89, "duration_seconds": 12.3}
  },
  "enrich_cache": {"hits": 8, "misses": 4, "size_after": 234},
  "totals": {"final_count": 89}
}
```

Why JSONL committed:
- One file, no schema migrations, easy to grep/jq.
- Diffs in git history give a free audit trail.
- Bounded growth: ~1 KB per daily run = ~365 KB/year. Fine.
- If it ever balloons, rotation is trivial later (cap to last 90 entries).

### Enrichment cache

`data/enrich_cache.json` — committed JSON map, keyed by URL.

```json
{
  "https://alignment.anthropic.com/2026/msm/": {
    "abstract": "...",
    "fetched_at": "2026-05-06T11:08:26Z",
    "status": "ok",
    "synthetic": false
  }
}
```

Lookup happens inside `enrich.py` *before* the HTTP fetch — wrap `_fetch_abstract_from_url(url)` (line 444) so it consults the cache first, returns the cached value on hit, and writes successful new results back on miss. The wrapper is the only call site that touches the cache; `_enrich_single` and `enrich_abstracts` stay unchanged in shape.

TTL policy:
- **Successful, non-synthetic** entries: cached indefinitely. Paper abstracts are essentially immutable once published.
- **Failed or synthetic** entries: cached for 7 days, then re-attempted (so a transient 5xx doesn't permanently poison the cache).
- **Eviction**: prune entries >180 days old at end of each run. Bounds file size.

Why JSON, not SQLite:
- Stays inspectable, diffable, mergeable.
- ~500 entries × ~1 KB = ~500 KB. Won't grow unbounded thanks to the 180-day eviction.
- No new dependency.

Why committed, not gitignored:
- CI runs always start with a warm cache → faster, fewer hits to upstream sites.
- Committed cache state is auditable (you can see when an entry was first cached / changed).
- The added commit churn is bounded (only new URLs each day) and is the same character as the existing `papers.json` churn.

### Health surfaced on the rendered site

`render.py` calls `compute_health()`, passes the result into the template. Template additions:

- **Banner at top** (only if any source is `broken`):
  > ⚠ N source(s) haven't returned data in over a week. [details ▾]
- **Collapsible "Pipeline health" footer section**, always present, listing every known source with:
  - status dot (●green / ●yellow / ●red / ●gray)
  - source name
  - last-success date
  - items fetched in last successful run

This makes silent breakage impossible to miss — broken scrapers show up on the public site itself.

### Snapshot tests for `render.py`

`tests/` directory:
- `tests/__init__.py`
- `tests/conftest.py` — pytest fixtures
- `tests/fixtures/papers.json` — small deterministic corpus (~5 papers, frozen dates)
- `tests/fixtures/run_log.jsonl` — minimal run log with mixed-health sources
- `tests/snapshots/index.html` — checked-in golden output
- `tests/test_render_snapshot.py` — single test that renders and compares

To make the output deterministic, `render.render()` must accept an optional `now: datetime | None` parameter (currently uses `datetime.now()` directly). Default behavior unchanged.

Test runs `render.render(papers, css, now=FROZEN_DATETIME, health=FIXTURE_HEALTH)` and byte-compares to the snapshot. To regenerate snapshots: `UPDATE_SNAPSHOTS=1 pytest`.

`pytest` added to `requirements.txt`.

## Wiring changes (file-by-file)

### Modified

- **`scripts/fetch.py`**
  - Construct `RunRecorder` at top of `main()`.
  - Pass it into each fetcher: change calls to `fetch_rss(cfg, recorder)`, `fetch_scraped(cfg, recorder)`, etc.
  - Wrap each pipeline stage with `recorder.record_stage(...)` timing.
  - Call `recorder.finalize()` before exit.

- **`scripts/fetchers/rss.py`** — `fetch_rss(feeds_config, recorder=None)`. For each feed, time the fetch and call `recorder.record_source(name, org, "rss", items, duration, status, error)`. `recorder=None` keeps the function callable from tests/scripts standalone.

- **`scripts/fetchers/scraper.py`** — same pattern, one `record_source` call per scraper target.

- **`scripts/fetchers/lesswrong.py`** — single source, one `record_source` call.

- **`scripts/fetchers/trending.py`** — record per-subreddit and per-HN-query as separate sources.

- **`scripts/fetchers/twitter.py`** — record per-account.

- **`scripts/dedup.py`** — `deduplicate(papers, recorder=None)`. Time the function, call `recorder.record_stage("dedup", in_count, out_count, duration)`.

- **`scripts/filter.py`** — same pattern: `record_stage("research_filter", ...)`.

- **`scripts/enrich.py`**
  - `enrich_abstracts(papers, recorder=None)`.
  - Load cache at top of `enrich_abstracts`. Wrap `_fetch_abstract_from_url` so it consults the cache first and writes successful results back. Save cache (with eviction of >180-day-old entries) at the end.
  - Record cache hits/misses and stage timing on the recorder.

- **`scripts/render.py`**
  - `render(papers, css, now=None, health=None)`.
  - Load `data/run_log.jsonl` and call `compute_health()` if `health is None`.
  - Pass `health`, `broken_sources_count`, `last_run_at` into template.

- **`templates/index.html.j2`** — add health banner (conditional) and collapsible health footer.

- **`static/style.css`** — minimal styles for the health section: status dots, banner, collapsible.

- **`requirements.txt`** — add `pytest`.

- **`CLAUDE.md`** — document the new artifacts (`run_log.jsonl`, `enrich_cache.json`), how to run tests (`pytest`), and how to update snapshots.

### New

- `scripts/observability.py`
- `tests/__init__.py`, `tests/conftest.py`, `tests/test_render_snapshot.py`
- `tests/fixtures/papers.json`, `tests/fixtures/run_log.jsonl`
- `tests/snapshots/index.html`
- `data/run_log.jsonl` (created on first run)
- `data/enrich_cache.json` (created on first run)

## Data flow

```
fetch.py:main()
  ├─ recorder = RunRecorder()
  ├─ fetch_rss(cfg, recorder)           ──> recorder.record_source(...) × N feeds
  ├─ fetch_scraped(cfg, recorder)        ──> recorder.record_source(...) × M scrapers
  ├─ fetch_lesswrong(cfg, recorder)      ──> recorder.record_source(...)
  ├─ fetch_trending(cfg, recorder)       ──> recorder.record_source(...) × subreddits
  ├─ fetch_twitter(cfg, recorder)        ──> recorder.record_source(...) × accounts
  ├─ [date filter] ──> recorder.record_stage("date_filter", ...)
  ├─ deduplicate(papers, recorder)       ──> recorder.record_stage("dedup", ...)
  ├─ filter_papers(papers, recorder)     ──> recorder.record_stage("research_filter", ...)
  ├─ enrich_abstracts(papers, recorder)  ──> recorder.record_stage("enrich", ...)
  │                                          + recorder.record_enrich_cache(...)
  └─ recorder.finalize()                 ──> appends to data/run_log.jsonl

render.py:render()
  ├─ load run_log.jsonl
  ├─ health = compute_health(window=7)
  └─ template renders banner + footer health section
```

## Edge cases

- **First run, no log history**: `compute_health()` returns `unknown` for all sources. No banner shown. No false positives during rollout.
- **Cache file missing or corrupt**: `enrich.py` treats it as empty, recreates on save. Single warning logged.
- **A previously-known source removed from config**: it ages out of `compute_health()` once it stops appearing in recent run entries (only sources seen in any of the last `window` runs are returned).
- **Run that fails midway**: `finalize()` is wrapped in `try/finally` in `fetch.py:main()` so a partial log entry is still written. Status field on the run captures the failure.
- **Snapshot tests on Windows line endings**: write golden file with explicit `\n` and read both sides as bytes, normalizing CRLF.
- **Time zone in snapshot tests**: frozen datetime is timezone-aware UTC; `compute_week_range` is called with `now=` injected.

## Implementation order

The sub-features have dependencies; implement in this order so each step is independently verifiable:

1. **`observability.py` skeleton** — `RunRecorder` with no-op methods, JSONL writer. Wire into `fetch.py` to produce a first run-log entry. (Foundation; no behavior change.)
2. **Per-source instrumentation** — add `recorder` param to all 5 fetchers, record per-source. Verify a real run produces a populated log entry.
3. **Per-stage instrumentation** — instrument `dedup`, `filter`, `enrich`, `date_filter`.
4. **`compute_health()`** — read log tail, return health dict. Unit-test with fixture data.
5. **Enrichment cache** — load/lookup/save inside `enrich.py`. Independent; can be done in parallel with #4 if desired.
6. **Render-side health UI** — surface banner + footer, inject `now` for testability.
7. **Snapshot tests** — last, so the snapshot captures the new health UI too. Add `pytest` to requirements.
8. **CLAUDE.md update** — document new artifacts and test commands.

## What this design deliberately does NOT include

- **No alerting** beyond the on-site banner. If you want email/Slack alerts later, the `run_log.jsonl` is a clean substrate for a separate watcher.
- **No metric retention strategy** beyond "let it grow." Revisit at 1000+ entries (~3 years).
- **No semantic dedup hooks**. That's Phase 3 of the larger plan; the recorder will accommodate a new `record_stage("semantic_dedup", ...)` call without schema change when Phase 3 lands.
- **No cache for fetcher pages**, only enrichment URLs. RSS feeds and scraper landing pages must hit upstream every run.
- **No structured logging library** (structlog, loguru). The standard `logging` module continues handling human-readable logs; `RunRecorder` handles structured run state. Keeping these separate avoids a dependency and a refactor.

## Acceptance criteria

After implementation:

- [ ] `python scripts/fetch.py` produces a new line in `data/run_log.jsonl` containing per-source and per-stage stats.
- [ ] `python scripts/fetch.py` populates `data/enrich_cache.json` and a second run shows `cache.hits > 0`.
- [ ] If a scraper is broken (returns 0 items) for 7+ runs, the rendered site shows a warning banner.
- [ ] `pytest` passes against the snapshot.
- [ ] `pytest` regenerates a sane snapshot when run with `UPDATE_SNAPSHOTS=1`.
- [ ] `python scripts/render.py` runs cleanly when `data/run_log.jsonl` is absent (cold start).
