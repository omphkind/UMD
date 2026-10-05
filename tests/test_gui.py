"""Exercise Qt controls against the actual asynchronous queue service."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from copy import deepcopy
from pathlib import Path
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import threading

import pytest
from PySide6.QtWidgets import QApplication

from app.core.config import Settings, SettingsStore
from app.tasks.queue import QueueService
from app.ui.formats import quality_choices, subtitle_choices
from app.ui.window import MainWindow, select

MEDIA = {"id": "fixture", "source": "generic", "url": "https://example.org/media.mp4",
         "title": "Тестовое видео", "original_title": "Test video", "duration": 125,
         "formats": [{"format_id": "hd", "height": 720, "vcodec": "h264", "acodec": "aac", "ext": "mp4"},
                     {"format_id": "audio", "vcodec": "none", "acodec": "opus", "ext": "webm"}],
         "subtitles": {"ru": [{"ext": "vtt"}]}, "automatic_captions": {"en": [{"ext": "vtt"}]}}


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(["UMD-tests"])


def wait(qt_app, predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("GUI worker did not deliver expected result")


class Resolver:
    def analyze(self, url):
        if url == "invalid":
            raise ValueError("Неверная ссылка")
        return deepcopy(MEDIA)


class Engine:
    def download(self, analysis, options, on_progress=None, control=None):
        on_progress({"stage": "downloading", "progress": 25, "speed": 1024, "eta": 2})
        folder = Path(options["output_path"])
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "fixture.txt"
        target.write_text("test fixture", encoding="utf-8")
        return {"files": [str(target)]}


@pytest.fixture
def window(qt_app, tmp_path):
    store = SettingsStore(tmp_path)
    store.save(Settings(localization=False, output_path=str(tmp_path / "output")))
    queue = QueueService(store.load(), tmp_path, resolver=Resolver(), engine_factory=lambda settings: Engine())
    widget = MainWindow(store, queue)
    widget.quick_environment_check = lambda: None
    widget.show()
    yield widget
    widget.close()
    qt_app.processEvents()


def test_analyze_and_enqueue_are_actual_background_operations(qt_app, window):
    window.url.setPlainText(MEDIA["url"])
    window.analyze_button.click()
    assert not window.analyze_button.isEnabled()
    wait(qt_app, lambda: window.analysis is not None)
    assert window.media_title.text() == MEDIA["title"]
    assert window.quality.findData("720") >= 0
    assert window.quality.findData("1080") == -1
    window.download_button.click()
    wait(qt_app, lambda: window.queue.snapshot() and window.queue.snapshot()[0]["status"] == "Completed")
    window.poll()
    assert window.pages.currentIndex() == 1
    assert window.queue_table.rowCount() == 1
    assert window.library.rowCount() == 1
    assert window.queue.snapshot()[0]["options"]["media_type"] == "video"
    window.queue_table.selectRow(0)
    window.copy_selected_url()
    assert QApplication.clipboard().text() == MEDIA["url"]


def test_failed_analysis_reenables_button_and_keeps_download_disabled(qt_app, window):
    window.url.setPlainText("invalid")
    window.analyze_button.click()
    wait(qt_app, lambda: window.analyze_button.isEnabled())
    assert "Неверная ссылка" in window.analysis_status.text()
    assert not window.download_button.isEnabled()


def test_real_capabilities_drive_choices_and_settings_roundtrip(window):
    window.show_analysis(deepcopy(MEDIA))
    select(window.kind, "audio")
    assert window.container.findData("mp3") >= 0
    assert not window.audio.isEnabled()
    select(window.kind, "subtitles")
    assert window.subtitles.findData("ru") >= 0
    assert window.subtitle_format.isEnabled()
    window.setting_fields["include_shorts"].setChecked(True)
    window.setting_fields["locale"].setText("en-US")
    window.save_settings()
    persisted = window.store.load()
    assert persisted.include_shorts and persisted.locale == "en-US"
    assert persisted.download_type == "subtitles"


def test_format_details_hidden_by_default_and_no_fictional_quality():
    choices = quality_choices(MEDIA)
    assert choices == [("Лучшее доступное", "best"), ("720p", "720")]
    assert all("ID" not in title for title, _ in choices)
    assert any("ID hd" in title for title, _ in quality_choices(MEDIA, advanced=True))
    assert quality_choices({"formats": []}) == []
    assert ("English · автоматически", "en") in subtitle_choices(MEDIA)


def test_editing_url_invalidates_formats_and_discards_old_result(window):
    window.url.setPlainText(MEDIA["url"])
    window.analyzed_urls = window.input_urls()
    window.analysis_request = "old-request"
    window.show_analysis(deepcopy(MEDIA))
    assert window.download_button.isEnabled()
    window.url.setPlainText("https://example.org/other.mp4")
    assert window.analysis is None and not window.download_button.isEnabled()
    window.handle_backend_event({"type": "analysis_ready", "request_id": "old-request", "analysis": deepcopy(MEDIA)})
    assert window.analysis is None


def test_gui_analyze_download_with_real_extractor_and_executable(qt_app, tmp_path):
    root = Path(__file__).resolve().parents[1]
    if not (root / "tools" / "ffmpeg.exe").is_file() or not (root / "tools" / "yt-dlp.exe").is_file():
        pytest.skip("Bundled Windows tools unavailable")
    from app.downloader.ffmpeg import FFmpegProcessor
    web = tmp_path / "web"
    web.mkdir()
    source = web / "sample.mp4"
    processor = FFmpegProcessor(Settings(), root)
    processor.run(["-f", "lavfi", "-i", "color=c=blue:s=128x72:r=10:d=0.5",
                   "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", "-c:v", "libx264",
                   "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)])
    (web / "index.html").write_text('<html><head><title>Actual GUI download</title></head>'
                                     '<body><video src="sample.mp4"></video></body></html>', encoding="utf-8")
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(web)))
    except OSError as error:
        pytest.skip("Local HTTP bind unavailable: " + str(error))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    store = SettingsStore(tmp_path / "data")
    store.save(Settings(localization=False, output_path=str(tmp_path / "downloads")))
    widget = MainWindow(store)
    widget.quick_environment_check = lambda: None
    widget.show()
    try:
        widget.url.setPlainText(f"http://127.0.0.1:{server.server_port}/index.html")
        widget.analyze_button.click()
        # Navigation while extraction runs verifies that the event loop is usable.
        widget.nav[4].click()
        assert widget.pages.currentIndex() == 4
        wait(qt_app, lambda: widget.analysis is not None, timeout=30)
        widget.nav[0].click()
        select(widget.container, "original")
        widget.download_button.click()
        wait(qt_app, lambda: widget.queue.snapshot() and all(task["status"] in {"Completed", "Failed", "Cancelled"}
                                                           for task in widget.queue.snapshot()), timeout=30)
        task = widget.queue.snapshot()[0]
        assert task["status"] == "Completed", task.get("error")
        target = next(Path(file) for file in task["files"] if file.endswith(".mp4"))
        assert hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(source.read_bytes()).digest()
        widget.poll()
        assert widget.library.rowCount() == 1
        assert widget.queue_table.rowCount() == 1
    finally:
        widget.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
