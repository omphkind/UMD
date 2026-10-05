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

STATUSES = {"Queued", "Analyzing", "Downloading", "Processing", "Completed", "Failed", "Paused", "Cancelled"}
ACTIVE = {"Analyzing", "Downloading", "Processing"}


@dataclass
class DownloadControl:
    pause: threading.Event = field(default_factory=threading.Event)
    cancel: threading.Event = field(default_factory=threading.Event)


def _options(settings: Settings, values=None, output_dir=None) -> dict:
    result = {"media_type": settings.download_type, "quality": settings.download_quality,
              "container": settings.download_container, "audio": settings.download_audio,
              "subtitles": settings.download_subtitles, "thumbnail": settings.download_thumbnail,
              "subtitle_format": "srt", "chapters": True, "output_path": settings.output_path}
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
        prefs["download_" + key] = result[option]
    Settings.from_dict(prefs)
    if result["subtitle_format"] not in {"vtt", "srt", "ass"} or not isinstance(result["chapters"], bool):
        raise ConfigurationError("Некорректные параметры субтитров или глав.")
    if not isinstance(result["output_path"], str) or not result["output_path"]:
        raise ConfigurationError("Укажите папку загрузки.")
    result["output_path"] = str(Path(result["output_path"]).expanduser().resolve())
    return result


class QueueService:
    """One download worker and one analyze worker keep all network work off GUI.

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
        self._analyzer = threading.Thread(target=self._analyze_work, name="UMD-analysis", daemon=True)
        self._analyzer.start()

    def analyze(self, url: str) -> str:
        if not isinstance(url, str) or not url.strip():
            raise ConfigurationError("Укажите URL для анализа.")
        with self._mutex:
            self._ensure_open()
            request_id = uuid4().hex
            self._analysis_controls[request_id] = DownloadControl()
            self._requests.put((request_id, url.strip(), self.settings.to_dict()))
        self._emit({"type": "analysis_started", "request_id": request_id, "url": url.strip()})
        return request_id

    def enqueue(self, analysis: dict, options=None, output_dir=None, *, _settings=None) -> list[dict]:
        if not isinstance(analysis, dict):
            raise ConfigurationError("Сначала выполните Analyze.")
        prefs = Settings.from_dict((_settings or self.settings).to_dict())
        normalized = _options(prefs, options, output_dir)
        if analysis.get("media_type") == "playlist" and not analysis.get("entries"):
            raise ConfigurationError("Источник не содержит доступных объектов.")
        entries = analysis.get("entries") or [analysis]
        if not isinstance(entries, list):
            raise ConfigurationError("Источник вернул некорректный список объектов.")
        added = []
        with self._condition:
            self._ensure_open()
            identities = {task["dedup_key"] for task in self._tasks.values()}
            for entry in entries:
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
                fingerprint = hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
                dedup_key = f"{source}:{media_id}:{fingerprint}"
                if dedup_key in identities:
                    continue
                item.update(source=source, id=media_id, url=url)
                task = {"id": uuid4().hex, "dedup_key": dedup_key, "source": source,
                        "media_id": media_id, "url": url, "title": str(item.get("title") or media_id),
                        "thumbnail": item.get("thumbnail") or "", "status": "Queued", "stage": "Queued",
                        "progress": 0.0, "speed": 0.0, "eta": None, "files": [], "error": None,
                        "options": deepcopy(normalized), "settings": prefs.to_dict(),
                        "analysis": item, "needs_analysis": bool(item.get("needs_analysis")) or bool(analysis.get("entries") and not item.get("formats")),
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

    def cancel_analysis(self, request_id=None):
        with self._mutex:
            for key, control in self._analysis_controls.items():
                if request_id is None or key == request_id:
                    control.cancel.set()

    def snapshot(self, *, compact=False) -> list[dict]:
        with self._mutex:
            if compact:
                return deepcopy([{key: value for key, value in task.items()
                                  if key not in {"analysis", "settings", "metadata", "dedup_key"}}
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
                self._requests.put(None)
        deadline = time.monotonic() + max(0.0, timeout)
        self._thread.join(max(0.0, deadline - time.monotonic()))
        if hasattr(self, "_analyzer"):
            self._analyzer.join(max(0.0, deadline - time.monotonic()))
        return not self._thread.is_alive() and not getattr(self, "_analyzer", self._thread).is_alive()

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
                while not self._stop.is_set():
                    with self._condition:
                        queued = next((task for task in self._tasks.values() if task["status"] == "Queued" and task["id"] not in self._controls), None)
                        if queued is None:
                            self._condition.wait(0.5)
                            continue
                        control = DownloadControl()
                        self._controls[queued["id"]] = control
                        self._set_status(queued, "Analyzing" if queued["needs_analysis"] else "Downloading")
                        self._save()
                        task = deepcopy(queued)
                    self._execute(task, control)
        except Exception as exc:
            self._startup_error = exc if isinstance(exc, UmdError) else StorageError(f"Не удалось открыть очередь: {exc}")
            self._stop.set()
            self._requests.put(None)
            self._ready.set()
            self._emit({"type": "queue_error", "error": str(self._startup_error)})

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
                            children = self.enqueue(analysis, task["options"], _settings=settings)
                            self._skip(self._tasks[key], "playlist_expanded")
                            self._save()
                            self._emit({"type": "playlist_expanded", "task_id": key,
                                        "task_ids": [child["id"] for child in children]})
                        return
                with self._condition:
                    current = self._tasks[key]
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
            engine = self._get_engine(settings)
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
            status = "Processing" if stage.lower() in {"processing", "merging", "postprocessing", "converting"} else "Downloading"
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
            request_id, url, prefs = request
            try:
                with self._mutex:
                    control = self._analysis_controls[request_id]
                if control.cancel.is_set():
                    continue
                analysis = self._analyze(self._get_resolver(Settings.from_dict(prefs)), url, control)
                if not self._stop.is_set():
                    self._emit({"type": "analysis_cancelled", "request_id": request_id} if control.cancel.is_set() else {"type": "analysis_ready", "request_id": request_id, "analysis": analysis})
            except Exception as exc:
                if not self._stop.is_set():
                    self._emit({"type": "analysis_cancelled", "request_id": request_id} if control.cancel.is_set() else {"type": "analysis_failed", "request_id": request_id, "error": str(exc)})
            finally:
                with self._mutex:
                    self._analysis_controls.pop(request_id, None)

    @staticmethod
    def _analyze(resolver, url, control):
        # Injectable lightweight resolvers may implement only analyze(url).
        # Production SourceResolver receives cancellation for its subprocess.
        signature = inspect.signature(resolver.analyze)
        accepts_control = "control" in signature.parameters or any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())
        result = resolver.analyze(url, control=control) if accepts_control else resolver.analyze(url)
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

    def _get_engine(self, settings):
        if self._engine_factory is not None:
            return self._engine_factory(settings)
        from app.downloader.engine import YtDlpDownloader
        return YtDlpDownloader(settings)

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
                except (ValueError, TypeError, UmdError) as exc:
                    raise StorageError("Некорректные параметры задачи очереди; исходный файл сохранён.") from exc
                if task["id"] in self._tasks or any(item["dedup_key"] == task["dedup_key"] for item in self._tasks.values()):
                    raise StorageError("Повторяющиеся задачи в файле очереди; исходный файл сохранён.")
                Settings.from_dict(task["settings"])
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
