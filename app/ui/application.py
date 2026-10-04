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


def run_task(data_dir: Path, defaults: Settings, operation: str, urls=None):
    store = ProgressStore(data_dir / "UMD_PROGRESS.json")
    # Keep the task snapshot and its processor settings in one transaction,
    # including potentially slow tool/browser startup.
    with store.lock():
        return _run_locked(store, data_dir, defaults, operation, urls)


def _run_locked(store, data_dir, defaults, operation, urls):
    settings = task_settings(store, defaults, operation)
    source = YouTubeSource(settings)
    source.downloader.check()
    if hasattr(source.downloader, "runtime_version"):
        source.downloader.runtime_version()
    output = Path(settings.output_path or data_dir / "output").expanduser()

    def report(item):
        print(f"[{item.number:03d}] {item.media_id} | {item.status.upper()}"
              + (f" — {item.error_reason or item.skip_reason}" if item.error_reason or item.skip_reason else ""))
        if item.status == "success":
            print(f"TITLE: {item.title}\nORIGINAL: {item.original_title}\nOVERVIEW: {item.overview}\n")
        progress = store.load()
        if progress:
            export_all(progress, output)

    with ExitStack() as stack:
        localizer = None
        if settings.localization:
            from app.localization.youtube import YouTubeLocalizer
            try:
                localizer = stack.enter_context(YouTubeLocalizer(settings))
            except (UmdError, RuntimeError, OSError) as error:
                print(f"Локализация недоступна: {error}. Будут использованы оригинальные метаданные.")
                log.debug("Localization startup failed", exc_info=settings.debug)
        processor = YouTubeProcessor(source, settings, localizer=localizer)
        service = TaskService(store, source, processor, settings, on_item=report)
        methods = {"new": lambda: service.new(urls or []), "resume": service.resume,
                   "update": service.update, "retry": service.retry_errors}
        progress = methods[operation]()
        paths = export_all(progress, output)
    print("Результат: " + ", ".join(f"{key}={value}" for key, value in progress.counts.items()))
    print("Экспорт: " + ", ".join(str(path) for path in paths.values()))
    return progress
