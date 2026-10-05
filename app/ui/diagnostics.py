"""Offline diagnostics and a packaged application smoke test."""

from pathlib import Path
import tempfile

from app.core.config import Settings
from app.core.errors import UmdError
from app.downloader.ytdlp import YtDlp
from app.export import export_all
from app.storage.progress import ProgressStore
from app.tasks.service import TaskService


def environment_check(settings: Settings, data_dir: Path) -> bool:
    results = []
    try:
        version = YtDlp(settings).check()
        results.append(("yt-dlp", True, version))
    except UmdError as error:
        results.append(("yt-dlp", False, str(error)))
    try:
        version = YtDlp(settings).runtime_version()
        results.append(("Deno / JavaScript runtime", True, version))
    except UmdError as error:
        results.append(("Deno / JavaScript runtime", False, str(error)))
    try:
        from app.downloader.ffmpeg import FFmpegProcessor
        version = FFmpegProcessor(settings).check()
        results.append(("FFmpeg / FFprobe", True, str(version)))
    except UmdError as error:
        results.append(("FFmpeg / FFprobe", False, str(error)))
    try:
        from app.localization.youtube import check_browser
        result = check_browser(settings)
        results.append(("Playwright / Chromium", result["ok"], result["message"]))
    except Exception:
        results.append(("Playwright / Chromium", False,
                        "Браузер недоступен. Используйте полную portable-сборку; для исходников: python -m playwright install chromium"))
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        output = Path(settings.output_path or data_dir / "output").expanduser()
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=data_dir), tempfile.TemporaryFile(dir=output):
            pass
        results.append(("Каталоги данных / экспорт", True, str(output)))
    except OSError:
        results.append(("Каталоги данных / экспорт", False, "Нет доступа к записи"))
    print("UMD Environment Check")
    for name, ok, detail in results:
        print(f"[{'OK' if ok else 'FAIL'}] {name}: {detail}")
    return all(ok for _, ok, _ in results)


def self_test():
    """Exercise persistence/resume/update/export in a temporary isolated task."""
    class FixtureSource:
        name = "youtube"
        ids = ["aaaaaaaaaaa", "bbbbbbbbbbb"]

        def discover(self, url):
            return [{"media_id": media_id, "url": f"https://www.youtube.com/watch?v={media_id}"}
                    for media_id in self.ids]

    class FixtureProcessor:
        def process(self, item):
            return {"title": f"Проверка {item.number}", "original_title": "Smoke test", "overview": "Описание"}

    with tempfile.TemporaryDirectory(prefix="umd-self-test-") as directory:
        path = Path(directory)
        store = ProgressStore(path / "UMD_PROGRESS.json")
        source = FixtureSource()
        service = TaskService(store, source, FixtureProcessor(), Settings(localization=False))
        progress = service.new("https://www.youtube.com/@umd-self-test")
        assert progress.counts["success"] == 2
        assert len(service.resume().items) == 2
        source.ids = ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
        progress = service.update()
        assert len(progress.items) == 3
        assert sorted(item.number for item in progress.items.values()) == [1, 2, 3]
        assert len(service.retry_errors().items) == 3
        paths = export_all(progress, path / "output")
        assert len(paths["urls"].read_text(encoding="utf-8").splitlines()) == 3
        assert "title=Проверка" in paths["metafin"].read_text(encoding="utf-8")
    print("UMD self-test: OK (progress, resume, update, stable numbering, exports)")
