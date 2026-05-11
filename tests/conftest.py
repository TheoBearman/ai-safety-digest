"""Shared pytest fixtures for the AI Safety Digest test suite."""

from __future__ import annotations

import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def fixture_papers() -> list[dict]:
    """A deterministic small corpus used by the render snapshot test."""
    with open(os.path.join(FIXTURES_DIR, "papers.json"), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def fixture_health() -> list[dict]:
    """A deterministic health snapshot with mixed statuses."""
    with open(os.path.join(FIXTURES_DIR, "health.json"), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def project_css() -> str:
    """The real stylesheet — keeps the snapshot honest about CSS changes."""
    with open(
        os.path.join(PROJECT_ROOT, "static", "style.css"), encoding="utf-8"
    ) as f:
        return f.read()
