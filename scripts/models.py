"""Data models and configuration utilities for the AI Safety Digest aggregator."""

from __future__ import annotations

import yaml
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import ClassVar


@dataclass
class Paper:
    """Represents a single paper or article from any source."""

    title: str
    authors: list[str]
    organization: str
    abstract: str
    url: str
    published_date: str  # ISO 8601 format
    source_type: str     # "rss" or "scrape"
    source_url: str
    fetched_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    VALID_SOURCE_TYPES: ClassVar[list[str]] = ["rss", "scrape"]

    def __post_init__(self):
        if self.source_type not in self.VALID_SOURCE_TYPES:
            raise ValueError(
                f"source_type must be one of {self.VALID_SOURCE_TYPES}, "
                f"got '{self.source_type}'"
            )

    def to_dict(self) -> dict:
        """Convert Paper to a plain dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Paper":
        """Create a Paper from a dictionary."""
        return cls(
            title=data["title"],
            authors=data["authors"],
            organization=data["organization"],
            abstract=data["abstract"],
            url=data["url"],
            published_date=data["published_date"],
            source_type=data["source_type"],
            source_url=data["source_url"],
            fetched_at=data.get(
                "fetched_at", datetime.now(timezone.utc).isoformat()
            ),
        )


def load_config(path: str) -> dict:
    """Read a YAML config file and return its contents as a dict."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
