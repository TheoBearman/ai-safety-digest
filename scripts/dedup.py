"""De-duplication utilities for paper lists."""

from __future__ import annotations

import logging
import re
import string
from difflib import SequenceMatcher

from scripts.models import Paper

logger = logging.getLogger(__name__)

# Precompile a translation table that strips all punctuation
_PUNCT_TABLE = str.maketrans("", "", string.punctuation)


def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, and collapse whitespace."""
    title = title.lower()
    title = title.translate(_PUNCT_TABLE)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def deduplicate(papers: list[Paper]) -> list[Paper]:
    """
    Remove duplicate and near-duplicate papers.

    Exact duplicates (same normalized title) are collapsed, keeping the
    entry with the longest abstract.  Near-duplicates are then detected
    using ``difflib.SequenceMatcher`` with a similarity ratio > 0.85.

    Parameters
    ----------
    papers : list[Paper]

    Returns
    -------
    list[Paper]
        Deduplicated list.
    """
    if not papers:
        return []

    # ------------------------------------------------------------------
    # Pass 0: exact-URL dedup (same URL = same paper regardless of title)
    # ------------------------------------------------------------------
    url_groups: dict[str, list[Paper]] = {}
    for paper in papers:
        url_key = paper.url.rstrip("/").lower()
        url_groups.setdefault(url_key, []).append(paper)

    url_deduped: list[Paper] = []
    url_dupes = 0
    for url_key, group in url_groups.items():
        if len(group) > 1:
            url_dupes += len(group) - 1
        best = max(group, key=lambda p: len(p.abstract))
        url_deduped.append(best)

    if url_dupes:
        logger.info("Removed %d exact-URL duplicates", url_dupes)

    # ------------------------------------------------------------------
    # Pass 1: exact-match dedup on normalized title
    # ------------------------------------------------------------------
    groups: dict[str, list[Paper]] = {}
    for paper in url_deduped:
        key = _normalize_title(paper.title)
        groups.setdefault(key, []).append(paper)

    # Keep the paper with the longest abstract in each exact-match group
    unique: list[Paper] = []
    exact_dupes = 0
    for key, group in groups.items():
        if len(group) > 1:
            exact_dupes += len(group) - 1
        best = max(group, key=lambda p: len(p.abstract))
        unique.append(best)

    if exact_dupes:
        logger.info("Removed %d exact-title duplicates", exact_dupes)

    # ------------------------------------------------------------------
    # Pass 2: near-duplicate detection via SequenceMatcher
    # ------------------------------------------------------------------
    # Build normalized titles once for the remaining papers
    norm_titles = [_normalize_title(p.title) for p in unique]
    keep = [True] * len(unique)

    for i in range(len(unique)):
        if not keep[i]:
            continue
        for j in range(i + 1, len(unique)):
            if not keep[j]:
                continue
            ratio = SequenceMatcher(None, norm_titles[i], norm_titles[j]).ratio()
            if ratio > 0.85:
                # Keep the one with the longer abstract
                if len(unique[j].abstract) > len(unique[i].abstract):
                    keep[i] = False
                    # Update norm_titles[j] stays, i is dropped
                    break  # i is now dropped; move on
                else:
                    keep[j] = False

    near_dupes = keep.count(False)
    if near_dupes:
        logger.info("Removed %d near-duplicate papers (ratio > 0.85)", near_dupes)

    result = [p for p, k in zip(unique, keep) if k]
    logger.info(
        "Deduplication complete: %d -> %d papers", len(papers), len(result)
    )
    return result
