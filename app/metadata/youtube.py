"""Fill the generic MediaItem without exposing YouTube logic to the task queue."""
from __future__ import annotations

import logging
from datetime import datetime

from app.core.errors import MediaError, SkipItem
from app.metadata.cleanup import clean_description, clean_title
from app.sources.youtube import is_members_only, shorts_signals

log = logging.getLogger(__name__)


class YouTubeProcessor:
    def __init__(self, source, settings, localizer=None):
        self.source = source
        self.settings = settings
        self.localizer = localizer

    def process(self, item) -> dict:
        if item.is_shorts and not self.settings.include_shorts:
            raise SkipItem("shorts")
        if item.is_members_only and not self.settings.include_members_only:
            raise SkipItem("members_only")
        try:
            raw = self.source.fetch(item.url)
        except MediaError as exc:
            if exc.reason == "members_only" and not self.settings.include_members_only:
                # Persist evidence even when yt-dlp cannot return JSON for this item.
                item.is_members_only = True
                raise SkipItem("members_only") from exc
            raise
        short_evidence = shorts_signals(raw) + shorts_signals(item.metadata)
        item.is_shorts = item.is_shorts or bool(short_evidence)
        item.is_members_only = item.is_members_only or is_members_only(raw)
        if item.is_shorts and not self.settings.include_shorts:
            raise SkipItem("shorts")
        if item.is_members_only and not self.settings.include_members_only:
            raise SkipItem("members_only")

        original_title = clean_title(raw.get("title") or item.title)
        original_description = str(raw.get("description") or "")
        title, description = original_title, original_description
        title_source, description_source = "original", "original"
        localized = {}
        if self.settings.localization and self.localizer is not None:
            try:
                localized = self.localizer.fetch(item.url)
                localized_title = clean_title(localized.get("title"))
                if localized_title:
                    title, title_source = localized_title, "youtube_localized"
                if localized.get("description_found", bool(localized.get("description"))):
                    description, description_source = localized.get("description", ""), "youtube_localized"
            except Exception as exc:
                log.warning("Локализация недоступна для %s; используются оригинальные данные: %s", item.media_id, exc)
                if self.settings.debug:
                    log.debug("Localization fallback", exc_info=True)
        if not title:
            raise MediaError("missing_metadata", "Источник не вернул название видео.")
        published = raw.get("upload_date") or raw.get("release_date") or item.published_at
        if isinstance(published, str) and len(published) == 8 and published.isdigit():
            try:
                published = datetime.strptime(published, "%Y%m%d").date().isoformat()
            except ValueError:
                pass
        metadata = {
            **item.metadata,
            "author": raw.get("channel") or raw.get("uploader"),
            "channel_id": raw.get("channel_id"), "channel_url": raw.get("channel_url"),
            "duration": raw.get("duration"), "thumbnail": raw.get("thumbnail"),
            "thumbnails": raw.get("thumbnails") or [], "chapters": raw.get("chapters") or [],
            "language": raw.get("language"), "availability": raw.get("availability"),
            "subtitles": raw.get("subtitles") or {}, "automatic_captions": raw.get("automatic_captions") or {},
            "shorts_signals": sorted(set(short_evidence)),
            "localized_values": {self.settings.locale: localized} if localized else {},
        }
        if self.settings.debug:
            log.debug("ID=%s original_title=%r localized=%r shorts=%s members=%s", item.media_id, original_title, localized, short_evidence, item.is_members_only)
        return {"title": title, "original_title": original_title,
                "overview": clean_description(description), "original_description": original_description,
                "title_source": title_source, "description_source": description_source,
                "is_shorts": item.is_shorts, "is_members_only": item.is_members_only,
                "published_at": published, "metadata": metadata}
