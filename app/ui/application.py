"""Build UI-independent services and connect progress events to console output."""

from contextlib import ExitStack
import logging
from pathlib import Path

from app.core.config import Settings
from app.core.errors import UmdError
from app.export import export_all
from app.metadata.youtube import YouTubeProcessor
from app.sources.youtube import YouTubeSource
from app.storage.progress import ProgressStore
from app.tasks.service import TaskService

log = logging.getLogger(__name__)


def task_settings(store: ProgressStore, defaults: Settings, operation: str) -> Settings:
    if operation == "new":
        return defaults
    progress = store.load()
    return Settings.from_dict(progress.settings) if progress else defaults


def run_task(data_dir: Path, defaults: Settings, operation: str, urls=None, *,
             on_event=None, cancel_event=None, quiet: bool = False):
    """Run metadata services with optional UI-independent event delivery.

    Call from a background thread for GUI use; cancellation takes effect before
    the next item and preserves the interrupted item as pending for resume.
    """
    store = ProgressStore(data_dir / "UMD_PROGRESS.json")
    # Keep the task snapshot and its processor settings in one transaction,
    # including potentially slow tool/browser startup.
    with store.lock():
        return _run_locked(store, data_dir, defaults, operation, urls,
                           on_event=on_event, cancel_event=cancel_event, quiet=quiet)


def _run_locked(store, data_dir, defaults, operation, urls, *, on_event=None,
                cancel_event=None, quiet=False):
    def emit(event):
        if on_event is not None:
            on_event(event)

    def say(message):
        if not quiet:
            print(message)

    if cancel_event is not None and cancel_event.is_set():
        emit({"type": "interrupted"})
        raise KeyboardInterrupt
    settings = task_settings(store, defaults, operation)
    source = YouTubeSource(settings)
    source.downloader.check()
    if hasattr(source.downloader, "runtime_version"):
        source.downloader.runtime_version()
    output = Path(settings.output_path or data_dir / "output").expanduser()

    def report(item):
        say(f"[{item.number:03d}] {item.media_id} | {item.status.upper()}"
              + (f" — {item.error_reason or item.skip_reason}" if item.error_reason or item.skip_reason else ""))
        if item.status == "success":
            say(f"TITLE: {item.title}\nORIGINAL: {item.original_title}\nOVERVIEW: {item.overview}\n")
        progress = store.load()
        if progress:
            export_all(progress, output)
        emit({"type": "item", "item": item.to_dict(), "counts": progress.counts if progress else {}})

    with ExitStack() as stack:
        localizer = None
        if settings.localization:
            from app.localization.youtube import YouTubeLocalizer
            try:
                localizer = stack.enter_context(YouTubeLocalizer(settings))
            except (UmdError, RuntimeError, OSError) as error:
                message = f"Локализация недоступна: {error}. Будут использованы оригинальные метаданные."
                say(message)
                emit({"type": "warning", "message": message})
                log.debug("Localization startup failed", exc_info=settings.debug)
        processor = YouTubeProcessor(source, settings, localizer=localizer)
        class ControlledProcessor:
            def process(self, item):
                if cancel_event is not None and cancel_event.is_set():
                    raise KeyboardInterrupt
                return processor.process(item)

        service = TaskService(store, source, ControlledProcessor(), settings, on_item=report)
        methods = {"new": lambda: service.new(urls or []), "resume": service.resume,
                   "update": service.update, "retry": service.retry_errors}
        try:
            progress = methods[operation]()
        except KeyboardInterrupt:
            emit({"type": "interrupted"})
            raise
        paths = export_all(progress, output)
    say("Результат: " + ", ".join(f"{key}={value}" for key, value in progress.counts.items()))
    say("Экспорт: " + ", ".join(str(path) for path in paths.values()))
    emit({"type": "completed", "counts": progress.counts, "history": progress.history,
          "paths": {key: str(path) for key, path in paths.items()}})
    return progress
