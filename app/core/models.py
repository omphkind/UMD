"""Stable media identities and the on-disk task schema."""

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any

STATUSES = {"pending", "processing", "success", "error", "skipped"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class MediaItem:
    source: str
    media_id: str
    url: str
    number: int
    status: str = "pending"
    title: str = ""
    original_title: str = ""
    overview: str = ""
    original_description: str = ""
    title_source: str = "original"
    description_source: str = "original"
    is_shorts: bool = False
    is_members_only: bool = False
    published_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    media_type: str = "video"
    collection_id: str = ""
    collection_type: str = "single"
    collection_index: int | None = None
    author: str = ""
    duration: float | None = None
    dimensions: dict[str, Any] = field(default_factory=dict)
    thumbnail: str = ""
    formats: list[dict[str, Any]] = field(default_factory=list)
    subtitles: dict[str, Any] = field(default_factory=dict)
    chapters: list[dict[str, Any]] = field(default_factory=list)
    source_metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    error_reason: str | None = None
    skip_reason: str | None = None

    @property
    def key(self) -> str:
        return f"{self.source}:{self.media_id}"

    @property
    def source_id(self) -> str:
        return self.media_id

    @property
    def id(self) -> str:
        return self.media_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaItem":
        if not isinstance(data, dict):
            raise ValueError("Media item must be an object")
        allowed = {f.name for f in fields(cls)}
        if set(data) - allowed:
            raise ValueError("Unknown media fields")
        item = cls(**data)
        if item.status not in STATUSES:
            raise ValueError("Unknown media status")
        if not isinstance(item.number, int) or isinstance(item.number, bool) or item.number < 1:
            raise ValueError("Invalid media number")
        if not all(isinstance(v, str) and v for v in (item.source, item.media_id, item.url)):
            raise ValueError("Missing media identity")
        if not isinstance(item.metadata, dict):
            raise ValueError("Metadata must be an object")
        if item.media_type not in {"video", "audio", "photo", "subtitle", "thumbnail", "metadata"}:
            raise ValueError("Invalid media type")
        if item.collection_type not in {"single", "album", "gallery", "playlist", "channel", "profile", "feed"}:
            raise ValueError("Invalid collection type")
        if not all(isinstance(v, dict) for v in (item.dimensions, item.subtitles, item.source_metadata)) or not all(isinstance(v, list) for v in (item.formats, item.chapters)):
            raise ValueError("Invalid media details")
        if not all(isinstance(v, str) for v in (item.collection_id, item.author, item.thumbnail)):
            raise ValueError("Invalid media text")
        for name in ("title", "original_title", "overview", "original_description", "title_source", "description_source", "published_at", "created_at", "updated_at"):
            if not isinstance(getattr(item, name), str):
                raise ValueError(f"Invalid text field {name}")
        if not isinstance(item.is_shorts, bool) or not isinstance(item.is_members_only, bool):
            raise ValueError("Invalid content flags")
        if not all(value is None or isinstance(value, str) for value in (item.error_reason, item.skip_reason)):
            raise ValueError("Invalid status reasons")
        return item


@dataclass
class Progress:
    source: str
    source_urls: list[str]
    settings: dict[str, Any]
    items: dict[str, MediaItem] = field(default_factory=dict)
    history: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "source_urls": self.source_urls,
            "settings": self.settings,
            "items": {key: item.to_dict() for key, item in self.items.items()},
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Progress":
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("Unsupported progress schema")
        source = data.get("source")
        urls, settings, history, items = (data.get(k) for k in ("source_urls", "settings", "history", "items"))
        if not isinstance(source, str) or not source:
            raise ValueError("Missing source")
        if not isinstance(urls, list) or not all(isinstance(u, str) and u for u in urls):
            raise ValueError("Invalid source URLs")
        if not all(isinstance(v, dict) for v in (settings, history, items)):
            raise ValueError("Invalid progress structure")
        parsed = {key: MediaItem.from_dict(value) for key, value in items.items()}
        if any(key != item.key for key, item in parsed.items()):
            raise ValueError("Media identity does not match its key")
        numbers = [item.number for item in parsed.values()]
        if len(numbers) != len(set(numbers)):
            raise ValueError("Duplicate media numbers")
        return cls(source, urls, settings, parsed, history)

    @property
    def counts(self) -> dict[str, int]:
        return {status: sum(item.status == status for item in self.items.values()) for status in sorted(STATUSES)}
