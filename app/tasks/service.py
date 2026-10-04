"""Task operations preserve source identities, permanent numbers and successes."""

from collections import Counter
from dataclasses import fields
import json
import logging
from typing import Any, Callable, Protocol
from uuid import uuid4

from app.core.config import Settings
from app.core.errors import MediaError, SkipItem, StorageError
from app.core.models import MediaItem, Progress, utc_now
from app.storage.progress import ProgressStore
from app.storage.atomic import write_json

log = logging.getLogger(__name__)


class Source(Protocol):
    name: str

    def discover(self, url: str) -> list[dict[str, Any]]: ...


class Processor(Protocol):
    def process(self, item: MediaItem) -> dict[str, Any]: ...


class TaskService:
    """Hold one OS lock from scan/load through the last item's durable save.

    new() runs a fresh queue; resume() selects pending/interrupted only;
    retry_errors() selects error only; update() processes new identities only.
    Task preferences are snapshotted, so saved tasks retain their own filters.
    Discovery ordering determines permanent numbering; dated entries are ordered
    by publication date and undated entries retain source order (reversed for
    oldest_first, since YouTube lists undated channel entries newest first).
    """

    def __init__(self, store: ProgressStore, source: Source, processor: Processor,
                 settings: Settings | None = None,
                 on_item: Callable[[MediaItem], None] | None = None):
        self.store, self.source, self.processor = store, source, processor
        self.settings = settings or Settings()
        self.on_item = on_item

    def new(self, urls: list[str] | str) -> Progress:
        urls = [urls] if isinstance(urls, str) else list(urls)
        urls = list(dict.fromkeys(url.strip() for url in urls if url.strip()))
        if not urls:
            raise MediaError("invalid_url", "Укажите хотя бы один URL.")
        with self.store.lock():
            previous = self.store.load()
            if previous is not None:
                archive = self.store.path.parent / "archive" / f"task-{utc_now().replace(':', '-')}-{uuid4().hex[:8]}.json"
                write_json(archive, previous.to_dict())
            progress = Progress(self.source.name, urls, Settings.from_dict(self.settings.to_dict()).to_dict())
            self._event(progress, "task_created")
            self.store.save(progress)
            self._scan(progress, "new")
            self.store.save(progress)
            return self._run(progress, list(progress.items.values()), "new")

    def resume(self) -> Progress:
        with self.store.lock():
            progress = self._load()
            self._recover(progress)
            pending = [item for item in progress.items.values() if item.status == "pending"]
            return self._run(progress, pending, "resume")

    def retry_errors(self) -> Progress:
        with self.store.lock():
            progress = self._load()
            self._recover(progress)
            failed = [item for item in progress.items.values() if item.status == "error"]
            return self._run(progress, failed, "retry_errors")

    def update(self) -> Progress:
        with self.store.lock():
            progress = self._load()
            self._recover(progress)
            added = self._scan(progress, "update")
            self.store.save(progress)
            return self._run(progress, added, "update")

    def _load(self) -> Progress:
        progress = self.store.load()
        if progress is None:
            raise StorageError("Предыдущая задача не найдена. Начните новую обработку.")
        if progress.source != self.source.name:
            raise MediaError("unsupported_source", "Источник предыдущей задачи не поддерживается этим обработчиком.")
        Settings.from_dict(progress.settings)
        return progress

    def _recover(self, progress: Progress) -> None:
        interrupted = [item for item in progress.items.values() if item.status == "processing"]
        if interrupted:
            for item in interrupted:
                item.status, item.updated_at = "pending", utc_now()
            self._event(progress, "recovered", count=len(interrupted))
            self._save(progress)

    def _discover(self, progress: Progress, operation: str) -> list[MediaItem]:
        settings = Settings.from_dict(progress.settings)
        discovered = []
        for url in progress.source_urls:
            discovered.extend(self.source.discover(url))
        # Most flat channel entries omit dates. Reverse that newest-first listing
        # when the user asks for oldest-first; known dates always sort explicitly.
        dated = [entry for entry in discovered if entry.get("published_at")]
        undated = [entry for entry in discovered if not entry.get("published_at")]
        dated.sort(key=lambda entry: str(entry["published_at"]), reverse=settings.order == "newest_first")
        if settings.order == "oldest_first":
            undated.reverse()
        discovered = dated + undated
        added = []
        next_number = max((item.number for item in progress.items.values()), default=0) + 1
        found_ids = set()
        allowed = {f.name for f in fields(MediaItem)} - {"source", "media_id", "number", "status", "created_at", "updated_at", "error_reason", "skip_reason"}
        for entry in discovered:
            media_id = str(entry.get("media_id", "")).strip()
            url = str(entry.get("url", "")).strip()
            if not media_id or not url:
                raise MediaError("invalid_metadata", "Источник вернул объект без ID или URL.")
            key = f"{self.source.name}:{media_id}"
            found_ids.add(key)
            if key in progress.items:
                continue
            values = {key: value for key, value in entry.items() if key in allowed}
            values["url"] = url
            values["published_at"] = values.get("published_at") or ""
            item = MediaItem(self.source.name, media_id, number=next_number, **values)
            try:
                MediaItem.from_dict(item.to_dict())
                json.dumps(item.to_dict(), allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise MediaError("invalid_metadata", "Источник вернул некорректные метаданные объекта.") from exc
            progress.items[item.key] = item
            added.append(item)
            next_number += 1
        now = utc_now()
        progress.history.setdefault("first_scanned", now)
        progress.history["last_checked"] = now
        if operation == "update":
            progress.history["last_updated"] = now
        progress.history["found_count"] = len(found_ids)
        progress.history["new_count"] = len(added)
        self._event(progress, "scan", operation=operation, found=len(found_ids), new=len(added))
        return added

    def _scan(self, progress: Progress, operation: str) -> list[MediaItem]:
        try:
            return self._discover(progress, operation)
        except (Exception, KeyboardInterrupt) as exc:
            reason = exc.reason if isinstance(exc, MediaError) else "interrupted" if isinstance(exc, KeyboardInterrupt) else "discovery_error"
            progress.history["last_error"] = {"reason": reason, "message": str(exc), "at": utc_now(), "operation": operation}
            self._event(progress, "scan_failed", operation=operation, reason=reason)
            self._save(progress)
            raise

    def _run(self, progress: Progress, selected: list[MediaItem], operation: str) -> Progress:
        settings = Settings.from_dict(progress.settings)
        self._event(progress, "run_started", operation=operation, selected=len(selected))
        self._save(progress)
        protected = {"source", "media_id", "url", "number", "status", "created_at", "updated_at", "error_reason", "skip_reason"}
        allowed = {f.name for f in fields(MediaItem)} - protected
        for item in sorted(selected, key=lambda item: item.number):
            item.status = "processing"
            item.error_reason = item.skip_reason = None
            item.updated_at = utc_now()
            self._save(progress)
            try:
                if item.is_shorts and not settings.include_shorts:
                    raise SkipItem("shorts")
                if item.is_members_only and not settings.include_members_only:
                    raise SkipItem("members_only")
                result = self.processor.process(item)
                if not isinstance(result, dict):
                    raise MediaError("invalid_metadata", "Обработчик вернул некорректные метаданные.")
                values = {name: value for name, value in result.items() if name in allowed}
                try:
                    candidate = MediaItem.from_dict({**item.to_dict(), **values})
                    json.dumps(candidate.to_dict(), allow_nan=False)
                except (TypeError, ValueError) as exc:
                    raise MediaError("invalid_metadata", "Обработчик вернул некорректные поля метаданных.") from exc
                for name in values:
                    setattr(item, name, getattr(candidate, name))
                if item.is_shorts and not settings.include_shorts:
                    raise SkipItem("shorts")
                if item.is_members_only and not settings.include_members_only:
                    raise SkipItem("members_only")
                item.status = "success"
            except SkipItem as exc:
                item.status, item.skip_reason = "skipped", exc.reason
            except KeyboardInterrupt:
                item.status, item.updated_at = "pending", utc_now()
                self._event(progress, "interrupted", media_id=item.media_id)
                self._save(progress)
                raise
            except Exception as exc:
                item.status = "error"
                item.error_reason = exc.reason if isinstance(exc, MediaError) else "processing_error"
                progress.history["last_error"] = {"media_id": item.media_id, "reason": item.error_reason, "message": str(exc), "at": utc_now()}
                log.debug("Media processing failed for %s", item.key, exc_info=True)
            item.updated_at = utc_now()
            self._save(progress)
            log.info("[%03d/%d] ID: %s | %s | %s%s", item.number, len(progress.items), item.media_id, item.url, item.status.upper(), f" ({item.error_reason or item.skip_reason})" if item.error_reason or item.skip_reason else "")
            if self.on_item:
                try:
                    self.on_item(item)
                except Exception:
                    log.warning("Не удалось обновить отображение результата.", exc_info=settings.debug)
        counts = progress.counts
        if not counts["error"] and not counts["pending"] and not counts["processing"]:
            progress.history["last_successful_run"] = utc_now()
        self._event(progress, "run_finished", operation=operation, counts=counts)
        self._save(progress)
        return progress

    def _save(self, progress: Progress) -> None:
        progress.history["counts"] = progress.counts
        progress.history["error_count"] = progress.counts["error"]
        progress.history["skipped_count"] = progress.counts["skipped"]
        progress.history["skip_reasons"] = dict(Counter(item.skip_reason for item in progress.items.values() if item.status == "skipped"))
        self.store.save(progress)

    @staticmethod
    def _event(progress: Progress, event: str, **details) -> None:
        progress.history.setdefault("events", []).append({"event": event, "at": utc_now(), **details})
