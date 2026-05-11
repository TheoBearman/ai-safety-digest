"""Run-level observability for the digest pipeline.

A single ``RunRecorder`` instance is created per invocation of ``fetch.py``.
Fetchers and pipeline stages call into it to record per-source and per-stage
metrics. At end of run, ``finalize()`` appends one JSON line to
``data/run_log.jsonl``.

``compute_health()`` reads the tail of that log and classifies each known
source as healthy / degraded / broken / unknown, which the renderer surfaces
on the site so silent fetcher failures cannot stay invisible.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterator

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
RUN_LOG_PATH = os.path.join(PROJECT_ROOT, "data", "run_log.jsonl")

# Health classification window (number of recent runs to look at).
HEALTH_WINDOW = 7
# A source is "degraded" if its last successful run was strictly more than
# this many runs ago; "broken" if it had no success in the full window.
HEALTHY_THRESHOLD = 3
# Need at least this many runs of history before classifying anything as
# degraded/broken — suppresses false positives during initial rollout.
MIN_RUNS_FOR_CLASSIFICATION = 3


@dataclass
class SourceRecord:
    name: str
    org: str
    type: str  # "rss" | "scrape" | "lesswrong" | "trending" | "twitter"
    items_fetched: int
    duration_seconds: float
    status: str  # "ok" | "error"
    error: str | None = None


@dataclass
class StageRecord:
    in_count: int
    out_count: int
    duration_seconds: float


@dataclass
class CacheRecord:
    hits: int
    misses: int
    size_after: int


@dataclass
class SourceHealth:
    name: str
    status: str  # "healthy" | "degraded" | "broken" | "unknown"
    last_success_at: str | None
    last_run_at: str | None
    last_items_fetched: int
    last_error: str | None


class RunRecorder:
    """Collects per-source and per-stage metrics for a single pipeline run."""

    def __init__(self, log_path: str = RUN_LOG_PATH) -> None:
        self._log_path = log_path
        self._started_at = datetime.now(timezone.utc)
        self._sources: list[SourceRecord] = []
        self._stages: dict[str, StageRecord] = {}
        self._enrich_cache: CacheRecord | None = None
        self._totals: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Recording API
    # ------------------------------------------------------------------
    def record_source(
        self,
        name: str,
        org: str,
        type: str,
        items_fetched: int,
        duration_seconds: float,
        status: str = "ok",
        error: str | None = None,
    ) -> None:
        self._sources.append(
            SourceRecord(
                name=name,
                org=org,
                type=type,
                items_fetched=items_fetched,
                duration_seconds=round(duration_seconds, 3),
                status=status,
                error=error,
            )
        )

    def record_stage(
        self,
        name: str,
        in_count: int,
        out_count: int,
        duration_seconds: float,
    ) -> None:
        self._stages[name] = StageRecord(
            in_count=in_count,
            out_count=out_count,
            duration_seconds=round(duration_seconds, 3),
        )

    def record_enrich_cache(self, hits: int, misses: int, size_after: int) -> None:
        self._enrich_cache = CacheRecord(
            hits=hits, misses=misses, size_after=size_after
        )

    def record_total(self, key: str, count: int) -> None:
        self._totals[key] = count

    @contextmanager
    def time_stage(self, name: str, in_count: int) -> Iterator["_StageTimer"]:
        """Context manager that auto-records a stage's duration and out_count.

        Usage:
            with recorder.time_stage("dedup", len(papers)) as t:
                papers = deduplicate(papers)
                t.out_count = len(papers)
        """
        timer = _StageTimer(in_count=in_count)
        start = time.perf_counter()
        try:
            yield timer
        finally:
            elapsed = time.perf_counter() - start
            out = timer.out_count if timer.out_count is not None else in_count
            self.record_stage(name, in_count, out, elapsed)

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    def finalize(self, status: str = "ok", error: str | None = None) -> dict:
        """Assemble the run entry and append it to the JSONL log.

        Safe to call from a ``finally`` block; uses ``status="error"`` to
        flag partial runs. Never raises — failures to persist are logged.
        """
        completed_at = datetime.now(timezone.utc)
        duration = (completed_at - self._started_at).total_seconds()
        run_id = self._started_at.strftime("%Y-%m-%dT%H:%M:%SZ")

        entry: dict = {
            "run_id": run_id,
            "started_at": self._started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "duration_seconds": round(duration, 3),
            "status": status,
            "sources": [asdict(s) for s in self._sources],
            "stages": {k: asdict(v) for k, v in self._stages.items()},
            "totals": dict(self._totals),
        }
        if self._enrich_cache is not None:
            entry["enrich_cache"] = asdict(self._enrich_cache)
        if error is not None:
            entry["error"] = error

        try:
            os.makedirs(os.path.dirname(self._log_path), exist_ok=True)
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            logger.info(
                "Wrote run-log entry %s (duration=%.1fs, sources=%d, stages=%d)",
                run_id, duration, len(self._sources), len(self._stages),
            )
        except OSError as exc:
            logger.warning("Failed to write run-log entry: %s", exc)
        return entry


@dataclass
class _StageTimer:
    in_count: int
    out_count: int | None = None


# ----------------------------------------------------------------------
# Health computation
# ----------------------------------------------------------------------

def _load_recent_runs(path: str, window: int) -> list[dict]:
    """Return up to ``window`` most-recent run entries from the JSONL log."""
    if not os.path.isfile(path):
        return []
    runs: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                runs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return runs[-window:]


def compute_health(
    log_path: str = RUN_LOG_PATH,
    window: int = HEALTH_WINDOW,
) -> dict[str, SourceHealth]:
    """Classify each source's recent health from the run log.

    A source is:
      - ``healthy``  if it had a successful run (items_fetched > 0) within
        the most recent ``HEALTHY_THRESHOLD`` runs.
      - ``degraded`` if its last successful run is older than that but still
        within the ``window``.
      - ``broken``   if it has no successful run in the entire window.
      - ``unknown``  if the log has fewer than ``MIN_RUNS_FOR_CLASSIFICATION``
        runs total (suppresses false positives during initial rollout).

    Only sources observed in at least one run within the window are returned;
    sources removed from config age out naturally.
    """
    runs = _load_recent_runs(log_path, window)
    if not runs:
        return {}

    have_enough_history = len(runs) >= MIN_RUNS_FOR_CLASSIFICATION

    # Walk runs newest-to-oldest, recording for each source:
    #   - the most-recent run it appeared in
    #   - the most-recent run with items_fetched > 0
    #   - how many runs ago that success was
    seen: dict[str, dict] = {}

    for offset, run in enumerate(reversed(runs)):
        run_completed = run.get("completed_at") or run.get("started_at")
        for src in run.get("sources", []):
            name = src.get("name")
            if not name:
                continue
            entry = seen.setdefault(
                name,
                {
                    "last_run_at": None,
                    "last_success_at": None,
                    "last_success_offset": None,
                    "last_items_fetched": 0,
                    "last_error": None,
                },
            )
            if entry["last_run_at"] is None:
                entry["last_run_at"] = run_completed
                entry["last_items_fetched"] = src.get("items_fetched", 0)
                entry["last_error"] = src.get("error")
            if (
                entry["last_success_at"] is None
                and src.get("items_fetched", 0) > 0
                and src.get("status", "ok") == "ok"
            ):
                entry["last_success_at"] = run_completed
                entry["last_success_offset"] = offset

    out: dict[str, SourceHealth] = {}
    for name, entry in seen.items():
        if not have_enough_history:
            status = "unknown"
        elif entry["last_success_offset"] is None:
            status = "broken"
        elif entry["last_success_offset"] < HEALTHY_THRESHOLD:
            status = "healthy"
        else:
            status = "degraded"

        out[name] = SourceHealth(
            name=name,
            status=status,
            last_success_at=entry["last_success_at"],
            last_run_at=entry["last_run_at"],
            last_items_fetched=entry["last_items_fetched"],
            last_error=entry["last_error"],
        )

    return out
