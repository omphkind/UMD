"""Durable background downloads with Qt-independent polling events."""

from copy import deepcopy
from dataclasses import asdict, dataclass, field, is_dataclass
import hashlib
import inspect
import json
import math
from pathlib import Path
from queue import Empty, Queue
import threading
import time
from typing import Any
from uuid import uuid4

from app.core.config import Settings
from app.core.errors import ConfigurationError, SkipItem, StorageError, UmdError
from app.core.models import utc_now
from app.storage.atomic import read_json, write_json
from app.storage.lock import FileLock

STATUSES = {"Queued", "Analyzing", "Preparing", "Renaming", "Downloading", "Processing", "Organizing", "Completed", "Failed", "Paused", "Cancelled"}
ACTIVE = {"Analyzing", "Preparing", "Renaming", "Downloading", "Processing", "Organizing"}


@dataclass
class DownloadControl:
    pause: threading.Event = field(default_factory=threading.Event)
    cancel: threading.Event = field(default_factory=threading.Event)


def original_audio_output_extension(item, quality="best"):
    """Share the downloader's actual format selection and audio extraction suffix."""
    from app.downloader.engine import DownloadOptions, YtDlpDownloader, original_audio_extension
    # Format selection is local metadata processing; no executable/network call.
    selection = DownloadOptions(media_type="audio", container="original", quality=quality)
    selector, _, _, _ = YtDlpDownloader(Settings())._select_formats(item, selection)
    chosen = next((fmt for fmt in item.get("formats", [])
                   if str(fmt.get("format_id")) == selector), {})
    return original_audio_extension(chosen)


def _options(settings: Settings, values=None, output_dir=None) -> dict:
    result = {"media_type": settings.download_type, "quality": settings.download_quality,
              "container": settings.download_container, "audio": settings.download_audio,
              "subtitles": settings.download_subtitles, "thumbnail": settings.download_thumbnail,
              "subtitle_format": "srt", "chapters": True, "output_path": settings.output_path,
              "metadata_preserve": settings.metadata_preserve, "embed_cover": settings.embed_cover,
              "audio_bitrate": settings.audio_bitrate, "target_path": "", "collision_policy": settings.collision_policy,
              "rename": {"enabled": settings.rename_before_download, "preset": settings.rename_default_preset,
                         "template": settings.rename_template, "rules": [], "start": settings.rename_number_start,
                         "step": settings.rename_number_step, "padding": settings.rename_number_padding,
                         "organization_enabled": settings.folder_organization, "folder_template": settings.folder_template,
                         "collision_policy": settings.collision_policy}}
    if is_dataclass(values):
        values = asdict(values)
    if values is not None:
        if not isinstance(values, dict):
            raise ConfigurationError("Параметры загрузки должны быть объектом.")
        values = dict(values)
        if "type" in values:
            values["media_type"] = values.pop("type")
        if set(values) - set(result):
            raise ConfigurationError("Неизвестные параметры загрузки.")
        result.update(values)
    if output_dir is not None:
        result["output_path"] = str(output_dir)
    prefs = settings.to_dict()
    for option, key in (("media_type", "type"), ("quality", "quality"), ("container", "container"),
                        ("audio", "audio"), ("subtitles", "subtitles"), ("thumbnail", "thumbnail")):
        prefs["download_" + key] = "video" if option == "media_type" and result[option] == "auto" else result[option]
    Settings.from_dict(prefs)
    for name in ("metadata_preserve", "embed_cover"):
        if not isinstance(result[name], bool):
            raise ConfigurationError(f"Параметр {name} должен быть логическим значением.")
    if not isinstance(result["audio_bitrate"], str) or not isinstance(result["rename"], dict) or not isinstance(result["target_path"], str):
        raise ConfigurationError("Некорректные параметры аудио или переименования.")
    if result["collision_policy"] not in {"ask", "skip", "overwrite", "append_number"}:
        raise ConfigurationError("Неизвестная политика совпадения имён.")
    Settings.from_dict({**prefs, "audio_bitrate": result["audio_bitrate"]})
    try:
        json.dumps(result["rename"], allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError("Некорректные правила переименования.") from exc
    if result["subtitle_format"] not in {"vtt", "srt", "ass"} or not isinstance(result["chapters"], bool):
        raise ConfigurationError("Некорректные параметры субтитров или глав.")
    if not isinstance(result["output_path"], str) or not result["output_path"]:
        raise ConfigurationError("Укажите папку загрузки.")
    result["output_path"] = str(Path(result["output_path"]).expanduser().resolve())
    if result["target_path"]:
        target = Path(result["target_path"]).expanduser().resolve()
        if not target.is_relative_to(Path(result["output_path"])):
            raise ConfigurationError("Итоговый файл должен находиться в папке загрузки.")
        result["target_path"] = str(target)
    return result


class QueueService:
    """A bounded download pool and an analyze worker keep network work off GUI.

    Snapshots/events are detached JSON dictionaries. A process lock covers the
    queue lifetime; a mutex serializes actions and durable snapshots. Download
    pause terminates the external process while retaining partial files. Resume
    restarts the engine, which continues those files. Interrupted states recover
    as Paused, so reopening UMD never silently restarts an interrupted transfer.
    """

    def __init__(self, settings: Settings, data_dir: str | Path, resolver=None,
                 engine_factory=None):
        self.settings = Settings.from_dict(settings.to_dict())
        self.data_dir = Path(data_dir)
        if not self.settings.output_path:
            self.settings.output_path = str(self.data_dir / "output")
        self.path = self.data_dir / "UMD_QUEUE.json"
        self._resolver = resolver
        self._engine_factory = engine_factory
        self._mutex = threading.RLock()
        self._condition = threading.Condition(self._mutex)
        self._events = Queue()
        self._requests = Queue()
        self._tasks = {}
        self._controls = {}
        self._workers = {}
        self._analysis_controls = {}
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._startup_error = None
        self._thread = threading.Thread(target=self._work, name="UMD-downloads", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            self._stop.set()
            raise StorageError("Не удалось запустить очередь загрузки.")
        if self._startup_error:
            raise self._startup_error
        self._analyzers = [threading.Thread(target=self._analyze_work, name=f"UMD-analysis-{index}", daemon=True)
                           for index in range(self.settings.gallery_max_concurrency)]
        self._analyzer = self._analyzers[0]
        for analyzer in self._analyzers:
            analyzer.start()

    def analyze(self, url: str, page=1, page_size=None) -> str:
        if not isinstance(url, str) or not url.strip():
            raise ConfigurationError("Укажите URL для анализа.")
        page_size = self.settings.gallery_page_size if page_size is None else page_size
        if (isinstance(page, bool) or not isinstance(page, int) or page < 1
                or isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 500):
            raise ConfigurationError("Некорректная страница или размер коллекции.")
        with self._mutex:
            self._ensure_open()
            request_id = uuid4().hex
            self._analysis_controls[request_id] = DownloadControl()
            self._requests.put((request_id, url.strip(), self.settings.to_dict(), page, page_size))
        self._emit({"type": "analysis_started", "request_id": request_id, "url": url.strip()})
        return request_id

    def enqueue(self, analysis: dict, options=None, output_dir=None, *, selected_ids=None, _settings=None) -> list[dict]:
        if not isinstance(analysis, dict):
            raise ConfigurationError("Сначала выполните Analyze.")
        prefs = Settings.from_dict((_settings or self.settings).to_dict())
        if options is None and analysis.get("media_type") in {"photo", "audio"}:
            kind = analysis["media_type"]
            options = {"media_type": kind, "container": getattr(prefs, f"default_{kind}_format"),
                       "quality": getattr(prefs, f"default_{kind}_quality")}
        normalized = _options(prefs, options, output_dir)
        if analysis.get("media_type") == "playlist" and not analysis.get("entries"):
            raise ConfigurationError("Источник не содержит доступных объектов.")
        entries = analysis.get("entries") or [analysis]
        if not isinstance(entries, list):
            raise ConfigurationError("Источник вернул некорректный список объектов.")
        if selected_ids is not None:
            if not isinstance(selected_ids, (list, tuple, set)) or not all(isinstance(value, str) for value in selected_ids):
                raise ConfigurationError("Выбор объектов должен содержать их ID.")
            selected_ids = set(selected_ids)
            entries = [item for item in entries if isinstance(item, dict) and
                       (str(item.get("id") or item.get("media_id") or "") in selected_ids or
                        f"{item.get('source') or analysis.get('source') or 'unknown'}:{item.get('id') or item.get('media_id')}" in selected_ids)]
        if not entries:
            return []
        added = []
        with self._condition:
            self._ensure_open()
            request_fingerprint = hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
            prior_requests = {task.get("request_key", task["dedup_key"]) for task in self._tasks.values()}
            entries = [entry for entry in entries if not isinstance(entry, dict) or
                       f"{entry.get('source') or analysis.get('source') or analysis.get('extractor') or 'unknown'}:{entry.get('id') or entry.get('media_id') or ''}:{request_fingerprint}" not in prior_requests]
            reserved = [task["options"].get("target_path") for task in self._tasks.values()
                        if task["status"] in {"Queued", "Paused", *ACTIVE} and task["options"].get("target_path")]
            planned = self._plan_batch(entries, analysis, normalized, prefs, reserved)
            identities = {task["dedup_key"] for task in self._tasks.values()}
            for entry, item_options, row in planned:
                if not isinstance(entry, dict):
                    raise ConfigurationError("Некорректный объект источника.")
                item = deepcopy(entry)
                source = str(item.get("source") or analysis.get("source") or analysis.get("extractor") or "unknown")
                media_id = str(item.get("id") or item.get("media_id") or "")
                url = str(item.get("url") or item.get("webpage_url") or "")
                if not media_id or not url:
                    raise ConfigurationError("Источник не вернул ID или URL объекта.")
                if item.get("is_shorts") and not prefs.include_shorts:
                    self._emit({"type": "task_skipped", "media_id": media_id, "reason": "shorts"})
                    continue
                if item.get("is_members_only") and not prefs.include_members_only:
                    self._emit({"type": "task_skipped", "media_id": media_id, "reason": "members_only"})
                    continue
                if row and row.get("skip"):
                    self._emit({"type": "task_skipped", "media_id": media_id, "reason": "filename_collision"})
                    continue
                fingerprint = hashlib.sha256(json.dumps(item_options, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
                dedup_key = f"{source}:{media_id}:{fingerprint}"
                if dedup_key in identities:
                    continue
                item.update(source=source, id=media_id, url=url)
                task = {"id": uuid4().hex, "dedup_key": dedup_key, "source": source,
                        "request_key": f"{source}:{media_id}:{request_fingerprint}", "request_fingerprint": request_fingerprint,
                        "media_id": media_id, "url": url, "title": str(item.get("title") or media_id),
                        "thumbnail": item.get("thumbnail") or "", "status": "Queued", "stage": "Queued",
                        "progress": 0.0, "speed": 0.0, "eta": None, "files": [], "error": None,
                        "media_type": item_options["media_type"],
                        "collection_id": str(item.get("collection_id") or analysis.get("collection_id") or ""),
                        "collection_index": item.get("collection_index") or item.get("gallery_index") or item.get("playlist_index"),
                        "options": deepcopy(item_options), "settings": prefs.to_dict(),
                        "requested_options": deepcopy(normalized),
                        "analysis": item, "needs_analysis": bool(item.get("needs_analysis")) or bool(analysis.get("entries") and not item.get("formats") and not item.get("backend") == "gallery-dl"),
                        "playlist_entry": bool(analysis.get("entries")) or bool(item.get("playlist_url") or item.get("playlist_index")),
                        "created_at": utc_now(), "updated_at": utc_now()}
                # Reject non-JSON results before inserting anything into the queue.
                try:
                    json.dumps(task, allow_nan=False)
                except (ValueError, TypeError) as exc:
                    raise ConfigurationError("Источник вернул несохраняемые метаданные.") from exc
                identities.add(dedup_key)
                added.append(deepcopy(task))
            for task in added:
                self._tasks[task["id"]] = task
            self._save()
            for task in added:
                self._emit({"type": "task_changed", "task": task})
            self._condition.notify_all()
            return deepcopy(added)

    @staticmethod
    def _plan_batch(entries, analysis, normalized, prefs, reserved_paths=None):
        prepared = []
        rename = normalized["rename"]
        rename_planning = rename.get("enabled") or rename.get("organization_enabled")
        for index, entry in enumerate(entries, 1):
            if not isinstance(entry, dict):
                raise ConfigurationError("Некорректный объект источника.")
            item = deepcopy(entry)
            options = deepcopy(normalized)
            actual_type = item.get("media_type") or analysis.get("media_type") or "video"
            if options["media_type"] == "auto" and actual_type not in {"video", "audio", "photo"}:
                if rename_planning:
                    raise ConfigurationError("Тип элементов плейлиста ещё неизвестен. Сначала выполните анализ объектов или выберите конкретный тип перед переименованием.")
                # Retain Auto until this flat entry has its own complete analysis.
                item.setdefault("collection", analysis.get("title") or "")
                item.setdefault("collection_id", analysis.get("collection_id") or analysis.get("id") or "")
                item.setdefault("collection_index", item.get("playlist_index") or index)
                prepared.append((item, options, None))
                continue
            if actual_type not in {"video", "audio", "photo"}:
                actual_type = "video"
            if options["media_type"] == "auto" or (actual_type == "photo" and options["media_type"] in {"video", "audio", "photo"}) or (options["media_type"] == "photo" and actual_type != "photo"):
                options["media_type"] = actual_type
                if actual_type == "photo":
                    options["container"] = prefs.default_photo_format
                    options["quality"] = prefs.default_photo_quality if normalized["media_type"] == "auto" else normalized["quality"]
                elif actual_type == "audio":
                    options["container"] = prefs.default_audio_format
                    options["quality"] = prefs.default_audio_quality
                else:
                    options["container"] = prefs.default_video_format
                    options["quality"] = prefs.default_video_quality
            kind = options["media_type"]
            if item.get("backend") in {"gallery-dl", "direct-http"} and kind in {"video", "audio"}:
                options["container"] = "original"
            item.setdefault("collection", analysis.get("title") or analysis.get("collection") or "")
            item.setdefault("collection_id", analysis.get("collection_id") or analysis.get("id") if analysis.get("entries") else "")
            item.setdefault("collection_index", item.get("gallery_index") or item.get("playlist_index") or index)
            item.setdefault("media_type", actual_type)
            extension = options["container"]
            if extension == "original":
                extracted_audio = (kind == "audio" or options["audio"] == "audio_only") and item.get("backend") not in {"gallery-dl", "direct-http"}
                if extracted_audio:
                    extension = original_audio_output_extension(item, options["quality"]) if item.get("formats") else None
                else:
                    extension = item.get("ext") or item.get("format") or Path(str(item.get("original_filename") or "")).suffix.lstrip(".")
                    if not extension:
                        formats = item.get("photo_formats") or item.get("formats") or []
                        extension = next((value.get("ext") for value in formats if isinstance(value, dict) and value.get("ext")), None)
                if not extension and rename_planning and kind in {"video", "audio", "photo"}:
                    raise ConfigurationError("Оригинальное расширение объекта ещё неизвестно. Выполните анализ объекта или выберите конкретный формат перед переименованием.")
                extension = extension or ("jpg" if kind == "photo" else "m4a" if kind == "audio" else "mp4")
            if kind == "metadata":
                extension = "json"
            elif kind == "subtitles":
                extension = options["subtitle_format"]
            elif kind == "thumbnail":
                extension = "jpg"
            item["ext"] = str(extension)
            prepared.append((item, options, None))
        if rename_planning:
            from app.rename.service import RenameService
            rows = RenameService().preview([item for item, _, _ in prepared],
                                          {**rename, "reserved_paths": reserved_paths or []}, normalized["output_path"])
            if len(rows) != len(prepared) or any(row.get("status") in {"error", "conflict"} for row in rows):
                raise ConfigurationError("Имена файлов содержат конфликты. Проверьте предпросмотр и выберите политику совпадений.")
            for index, row in enumerate(rows):
                item, options, _ = prepared[index]
                options["target_path"] = str(row["target_path"])
                options["collision_policy"] = row.get("collision_policy", rename.get("collision_policy", options["collision_policy"]))
                prepared[index] = item, options, row
        return prepared

    def update_file_paths(self, mapping):
        if not isinstance(mapping, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in mapping.items()):
            raise ConfigurationError("Переименования должны содержать исходные и итоговые пути.")
        resolved = {str(Path(key).resolve()): str(Path(value).resolve()) for key, value in mapping.items()}
        with self._condition:
            self._ensure_open()
            if any(task["status"] in ACTIVE and any(str(Path(path).resolve()) in resolved for path in task["files"]) for task in self._tasks.values()):
                raise ConfigurationError("Нельзя переименовать файл активной загрузки.")
            for task in self._tasks.values():
                task["files"] = [resolved.get(str(Path(path).resolve()), path) for path in task["files"]]
                target = task["options"].get("target_path")
                if target and str(Path(target).resolve()) in resolved:
                    task["options"]["target_path"] = resolved[str(Path(target).resolve())]
                self._emit({"type": "task_changed", "task": task})
            self._save()

    def cancel_analysis(self, request_id=None):
        with self._mutex:
            for key, control in self._analysis_controls.items():
                if request_id is None or key == request_id:
                    control.cancel.set()

    def snapshot(self, *, compact=False) -> list[dict]:
        with self._mutex:
            if compact:
                return deepcopy([{key: value for key, value in task.items()
                                  if key not in {"analysis", "settings", "metadata", "dedup_key", "requested_options"}}
                                 for task in self._tasks.values()])
            return deepcopy(list(self._tasks.values()))

    def poll_events(self, limit=1000) -> list[dict]:
        events = []
        for _ in range(limit):
            try:
                events.append(self._events.get_nowait())
            except Empty:
                break
        return events

    def pause(self, task_id=None):
        self._action(task_id, "Paused", {"Queued", *ACTIVE})

    def resume(self, task_id=None):
        self._action(task_id, "Queued", {"Paused"})

    def cancel(self, task_id=None):
        self._action(task_id, "Cancelled", {"Queued", "Paused", *ACTIVE})

    def retry(self, task_id):
        self._action(task_id, "Queued", {"Failed", "Cancelled"})

    def retry_failed(self):
        self._action(None, "Queued", {"Failed"})

    def pause_all(self):
        self.pause()

    def resume_all(self):
        self.resume()

    def cancel_all(self):
        self.cancel()

    def clear_completed(self):
        with self._condition:
            self._ensure_open()
            removed = [key for key, task in self._tasks.items() if task["status"] == "Completed"]
            for key in removed:
                del self._tasks[key]
            self._save()
            self._emit({"type": "queue_changed", "removed": removed})

    def shutdown(self, timeout=3.0) -> bool:
        """Persist Paused immediately and wait at most timeout for subprocess exit."""
        with self._condition:
            if not self._stop.is_set():
                self._stop.set()
                for task in self._tasks.values():
                    if task["status"] in ACTIVE or task["status"] == "Queued":
                        control = self._controls.get(task["id"])
                        if control:
                            control.pause.set()
                        self._set_status(task, "Paused")
                for control in self._analysis_controls.values():
                    control.cancel.set()
                self._save()
                self._condition.notify_all()
                for _ in self._analyzers:
                    self._requests.put(None)
        deadline = time.monotonic() + max(0.0, timeout)
        self._thread.join(max(0.0, deadline - time.monotonic()))
        for analyzer in getattr(self, "_analyzers", []):
            analyzer.join(max(0.0, deadline - time.monotonic()))
        return not self._thread.is_alive() and not any(analyzer.is_alive() for analyzer in getattr(self, "_analyzers", []))

    def _action(self, task_id, status, eligible):
        with self._condition:
            self._ensure_open()
            if task_id is not None and task_id not in self._tasks:
                raise ConfigurationError("Задача не найдена.")
            selected = [self._tasks[task_id]] if task_id is not None else list(self._tasks.values())
            for task in selected:
                if task["status"] not in eligible:
                    continue
                control = self._controls.get(task["id"])
                if control and status == "Paused":
                    control.pause.set()
                if control and status == "Cancelled":
                    control.cancel.set()
                if status == "Queued":
                    task["error"] = None
                self._set_status(task, status)
            self._save()
            self._condition.notify_all()

    def _work(self):
        try:
            with FileLock(Path(str(self.path) + ".lock")):
                with self._condition:
                    self._load()
                    self._ready.set()
                while True:
                    with self._condition:
                        if self._stop.is_set():
                            if not self._workers:
                                break
                            self._condition.wait(0.1)
                            continue
                        capacity = self.settings.simultaneous_downloads
                        queued = next((task for task in self._tasks.values() if task["status"] == "Queued" and task["id"] not in self._controls and task["id"] not in self._workers), None) if len(self._workers) < capacity else None
                        if queued is None:
                            self._condition.wait(0.5)
                            continue
                        control = DownloadControl()
                        self._controls[queued["id"]] = control
                        self._set_status(queued, "Analyzing" if queued["needs_analysis"] else "Downloading")
                        self._save()
                        task = deepcopy(queued)
                        worker = threading.Thread(target=self._run_download, args=(task, control), name=f"UMD-transfer-{task['id'][:8]}", daemon=True)
                        self._workers[task["id"]] = worker
                        worker.start()
        except Exception as exc:
            self._startup_error = exc if isinstance(exc, UmdError) else StorageError(f"Не удалось открыть очередь: {exc}")
            self._stop.set()
            for _ in getattr(self, "_analyzers", [None]):
                self._requests.put(None)
            self._ready.set()
            self._emit({"type": "queue_error", "error": str(self._startup_error)})

    def _run_download(self, task, control):
        try:
            self._execute(task, control)
        finally:
            with self._condition:
                self._workers.pop(task["id"], None)
                self._condition.notify_all()

    def _execute(self, task, control):
        key = task["id"]
        settings = Settings.from_dict(task["settings"])
        try:
            analysis = task["analysis"]
            if task["needs_analysis"]:
                analysis = self._analyze(self._get_resolver(settings), task["url"], control)
                if control.pause.is_set() or control.cancel.is_set() or self._stop.is_set():
                    return
                if analysis.get("entries") or analysis.get("media_type") == "playlist":
                    candidates = analysis.get("entries") or []
                    matched = next((entry for entry in candidates if str(entry.get("id") or entry.get("media_id") or "") == task["media_id"] and str(entry.get("source") or analysis.get("source") or "") == task["source"]), None)
                    if matched is not None:
                        # Generic HTML media entries share the container URL.
                        # Re-analysis returns that container again; choose the
                        # original item and retain its yt-dlp playlist selector.
                        previous = task["analysis"]
                        analysis = deepcopy(matched)
                        for field_name in ("playlist_url", "playlist_index"):
                            if field_name not in analysis and field_name in previous:
                                analysis[field_name] = previous[field_name]
                    elif task.get("playlist_entry") or task["analysis"].get("playlist_index") or task["analysis"].get("playlist_url"):
                        raise ConfigurationError("Исходный объект больше не найден в плейлисте.")
                    else:
                        # An unresolved extra URL can turn out to be a channel
                        # or playlist. Expand it once using its saved preferences.
                        with self._condition:
                            children = self.enqueue(analysis, task.get("requested_options", task["options"]), _settings=settings)
                            self._skip(self._tasks[key], "playlist_expanded")
                            self._save()
                            self._emit({"type": "playlist_expanded", "task_id": key,
                                        "task_ids": [child["id"] for child in children]})
                        return
                with self._condition:
                    current = self._tasks[key]
                    requested = current.get("requested_options", current["options"])
                    if requested.get("media_type") == "auto":
                        if analysis.get("media_type") not in {"video", "audio", "photo"}:
                            raise ConfigurationError("Источник вернул только метаданные; доступный тип медиа не определён.")
                        # Refresh media/format choices using the preferences saved
                        # with the request. Preserve an already confirmed batch name.
                        refresh = deepcopy(requested)
                        refresh["target_path"] = current["options"].get("target_path", "")
                        refresh["rename"] = {**refresh["rename"], "enabled": False, "organization_enabled": False}
                        prepared, refreshed_options, _ = self._plan_batch([analysis], analysis, refresh, settings)[0]
                        if refresh["target_path"] and Path(refresh["target_path"]).suffix.lstrip(".").lower() != str(prepared.get("ext") or "").lower():
                            raise ConfigurationError("Тип или формат объекта изменился после анализа. Обновите предпросмотр имени.")
                        refreshed_options["rename"] = deepcopy(requested["rename"])
                        current["options"] = refreshed_options
                        current["media_type"] = refreshed_options["media_type"]
                        task["options"] = deepcopy(refreshed_options)
                    for field in ("collection_id", "collection_index", "collection", "gallery_index"):
                        if task["analysis"].get(field) is not None:
                            analysis[field] = task["analysis"][field]
                    current["analysis"] = deepcopy(analysis)
                    current["needs_analysis"] = False
                    current["title"] = str(analysis.get("title") or current["title"])
                    current["thumbnail"] = analysis.get("thumbnail") or current["thumbnail"]
                    if current["status"] in ACTIVE:
                        self._set_status(current, "Downloading")
                    self._save()
            with self._condition:
                current = self._tasks[key]
                self._resolve_identity(current, analysis)
                self._save()
                if current["status"] == "Cancelled":
                    return
            reason = "shorts" if analysis.get("is_shorts") and not settings.include_shorts else "members_only" if analysis.get("is_members_only") and not settings.include_members_only else None
            if reason:
                with self._condition:
                    self._skip(self._tasks[key], reason)
                    self._save()
                return
            if control.pause.is_set() or control.cancel.is_set() or self._stop.is_set():
                return
            engine = self._get_engine(settings, analysis)
            result = engine.download(analysis, task["options"],
                                     on_progress=lambda values: self._progress(key, values), control=control)
            if not isinstance(result, dict) or not isinstance(result.get("files", []), list) or not all(isinstance(path, str) for path in result.get("files", [])) or not isinstance(result.get("metadata", {}), dict):
                raise ConfigurationError("Загрузчик вернул некорректный результат.")
            try:
                json.dumps(result, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ConfigurationError("Загрузчик вернул несохраняемые метаданные.") from exc
            with self._condition:
                current = self._tasks[key]
                current["files"] = list(result.get("files") or [])
                current["metadata"] = result.get("metadata") or {}
                if not self._stop.is_set() and current["status"] in ACTIVE:
                    current["progress"] = 100.0
                    current["speed"], current["eta"] = 0.0, 0.0
                    self._set_status(current, "Completed")
                self._save()
        except Exception as exc:
            with self._condition:
                current = self._tasks[key]
                reason = getattr(exc, "reason", "")
                if current["status"] in {"Paused", "Cancelled"} or self._stop.is_set() or (current["status"] == "Queued" and (control.pause.is_set() or control.cancel.is_set())):
                    pass  # An intentional interruption keeps the requested state.
                elif isinstance(exc, SkipItem):
                    self._skip(current, exc.reason)
                elif reason in {"paused", "cancelled"}:
                    self._set_status(current, "Paused" if reason == "paused" else "Cancelled")
                else:
                    current["error"] = str(exc)
                    self._set_status(current, "Failed")
                self._save()
        finally:
            with self._condition:
                self._controls.pop(key, None)
                self._condition.notify_all()

    def _progress(self, key, values):
        with self._condition:
            task = self._tasks.get(key)
            if task is None or task["status"] not in ACTIVE or self._stop.is_set():
                return
            stage = str(values.get("stage") or task["stage"])
            status = {"preparing": "Preparing", "renaming": "Renaming", "organizing": "Organizing", "analyzing": "Analyzing"}.get(stage.lower(),
                     "Processing" if stage.lower() in {"processing", "merging", "postprocessing", "converting"} else "Downloading")
            for field_name in ("progress", "speed", "eta"):
                if field_name in values:
                    value = values[field_name]
                    if value is None and field_name == "eta":
                        task[field_name] = None
                        continue
                    try:
                        value = float(value)
                    except (ValueError, TypeError):
                        value = 0.0
                    task[field_name] = max(0.0, value) if math.isfinite(value) else 0.0
                    if field_name == "progress":
                        task[field_name] = min(100.0, task[field_name])
            if isinstance(values.get("files"), list):
                task["files"] = [path for path in values["files"] if isinstance(path, str)]
            if task["status"] != status:
                self._set_status(task, status)
            task["stage"] = stage
            task["updated_at"] = utc_now()
            self._save()
            self._emit({"type": "progress", "task_id": key, "stage": task["stage"],
                        "progress": task["progress"], "speed": task["speed"],
                        "eta": task["eta"], "files": list(task["files"])})

    def _resolve_identity(self, task, analysis):
        source = str(analysis.get("source") or task["source"])
        media_id = str(analysis.get("id") or analysis.get("media_id") or task["media_id"])
        url = str(analysis.get("url") or task["url"])
        fingerprint = hashlib.sha256(json.dumps(task["options"], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        dedup_key = f"{source}:{media_id}:{fingerprint}"
        task.update(source=source, media_id=media_id, url=url)
        if task.get("request_fingerprint"):
            task["request_key"] = f"{source}:{media_id}:{task['request_fingerprint']}"
        duplicate = any(item["id"] != task["id"] and item["dedup_key"] == dedup_key for item in self._tasks.values())
        if duplicate:
            # Retain the attempted task in history without creating another
            # downloadable identity or colliding with the persisted unique key.
            task["dedup_key"] = "duplicate:" + task["id"]
            self._skip(task, "duplicate")
        else:
            task["dedup_key"] = dedup_key

    def _skip(self, task, reason):
        task["skip_reason"] = reason
        task["error"] = None
        self._set_status(task, "Cancelled")
        self._emit({"type": "task_skipped", "task_id": task["id"], "media_id": task["media_id"], "reason": reason})

    def _analyze_work(self):
        while not self._stop.is_set():
            request = self._requests.get()
            if request is None:
                break
            request_id, url, prefs, page, page_size = request
            try:
                with self._mutex:
                    control = self._analysis_controls[request_id]
                if control.cancel.is_set():
                    continue
                analysis = self._analyze(self._get_resolver(Settings.from_dict(prefs)), url, control, page, page_size)
                if not self._stop.is_set():
                    self._emit({"type": "analysis_cancelled", "request_id": request_id} if control.cancel.is_set() else {"type": "analysis_ready", "request_id": request_id, "analysis": analysis})
            except Exception as exc:
                if not self._stop.is_set():
                    self._emit({"type": "analysis_cancelled", "request_id": request_id} if control.cancel.is_set() else {"type": "analysis_failed", "request_id": request_id, "error": str(exc)})
            finally:
                with self._mutex:
                    self._analysis_controls.pop(request_id, None)

    @staticmethod
    def _analyze(resolver, url, control, page=1, page_size=80):
        # Injectable lightweight resolvers may implement only analyze(url).
        # Production SourceResolver receives cancellation for its subprocess.
        signature = inspect.signature(resolver.analyze)
        accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())
        kwargs = {key: value for key, value in {"control": control, "page": page, "page_size": page_size}.items()
                  if accepts_kwargs or key in signature.parameters}
        result = resolver.analyze(url, **kwargs)
        if not isinstance(result, dict):
            raise ConfigurationError("Источник вернул некорректный результат анализа.")
        try:
            json.dumps(result, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("Источник вернул несохраняемый результат анализа.") from exc
        return result

    def _get_resolver(self, settings):
        if self._resolver is not None:
            return self._resolver
        from app.sources.resolver import SourceResolver
        return SourceResolver(settings)

    def _get_engine(self, settings, analysis):
        if self._engine_factory is not None:
            return self._engine_factory(settings)
        from app.downloader.router import create_downloader
        return create_downloader(settings, analysis)

    def _load(self):
        if self.path.exists():
            data = read_json(self.path)
            if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("tasks"), list):
                raise StorageError("Повреждено состояние очереди; исходный файл сохранён.")
            for task in data["tasks"]:
                required = {"id", "dedup_key", "source", "media_id", "url", "title", "thumbnail", "status", "stage", "progress", "speed", "eta", "error", "options", "settings", "analysis", "needs_analysis", "files", "created_at", "updated_at"}
                if not isinstance(task, dict) or required - set(task) or task["status"] not in STATUSES:
                    raise StorageError("Повреждена задача очереди; исходный файл сохранён.")
                if not all(isinstance(task[field], str) for field in ("id", "dedup_key", "source", "media_id", "url", "title", "stage", "created_at", "updated_at")):
                    raise StorageError("Некорректные поля задачи очереди; исходный файл сохранён.")
                if not all(isinstance(task[field], dict) for field in ("options", "settings", "analysis")) or not isinstance(task["needs_analysis"], bool) or not isinstance(task["files"], list):
                    raise StorageError("Некорректные данные задачи очереди; исходный файл сохранён.")
                if not all(isinstance(task[field], (int, float)) and not isinstance(task[field], bool) for field in ("progress", "speed")) or not (task["eta"] is None or isinstance(task["eta"], (int, float))) or not all(isinstance(path, str) for path in task["files"]):
                    raise StorageError("Некорректный прогресс задачи; исходный файл сохранён.")
                try:
                    json.dumps(task, allow_nan=False)
                    _options(Settings.from_dict(task["settings"]), task["options"])
                    if "requested_options" in task:
                        if not isinstance(task["requested_options"], dict):
                            raise ValueError("Invalid original download request")
                        _options(Settings.from_dict(task["settings"]), task["requested_options"])
                except (ValueError, TypeError, UmdError) as exc:
                    raise StorageError("Некорректные параметры задачи очереди; исходный файл сохранён.") from exc
                if task["id"] in self._tasks or any(item["dedup_key"] == task["dedup_key"] for item in self._tasks.values()):
                    raise StorageError("Повторяющиеся задачи в файле очереди; исходный файл сохранён.")
                Settings.from_dict(task["settings"])
                task.setdefault("media_type", task["options"].get("media_type", "video"))
                task.setdefault("collection_id", str(task["analysis"].get("collection_id") or ""))
                task.setdefault("collection_index", task["analysis"].get("collection_index") or task["analysis"].get("playlist_index"))
                if task["status"] in ACTIVE:
                    task["status"] = task["stage"] = "Paused"
                    task["updated_at"] = utc_now()
                self._tasks[task["id"]] = task
            self._save()

    def _save(self):
        write_json(self.path, {"schema_version": 1, "updated_at": utc_now(), "tasks": list(self._tasks.values())}, backup=True)

    def _set_status(self, task, status):
        task["status"] = task["stage"] = status
        task["updated_at"] = utc_now()
        self._emit({"type": "task_changed", "task": deepcopy(task)})

    def _emit(self, event):
        self._events.put(deepcopy(event))

    def _ensure_open(self):
        if self._stop.is_set():
            raise StorageError("Очередь уже закрыта.")
