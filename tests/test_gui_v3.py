"""Beta 3 GUI flows with actual HTTP media, downloaders and filesystem renames."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from copy import deepcopy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading
import time

import pytest
from PySide6.QtCore import Qt, QTimer, QBuffer, QIODevice, QSize
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QMessageBox

from app.core.config import Settings, SettingsStore
from app.downloader.ffmpeg import FFmpegProcessor
from app.sources.gallery_dl_source import normalize_gallery_item
from app.sources.resolver import SourceResolver
from app.tasks.queue import QueueService
from app.ui.gallery_view import CollectionModel, GallerySelectionDialog
from app.ui.rename_dialog import RenameDialog
from app.ui.window import MainWindow, select

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(["UMD-Beta3-tests"])


def wait(app, predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("GUI did not deliver the expected asynchronous result")


def drive_modal(app, dialog_class, trigger, interact, timeout=20):
    """Operate the real nested Qt dialog event loop; never replace its exec()."""
    timer = QTimer()
    timer.setInterval(10)
    state = {"seen": False, "errors": [], "started": time.monotonic()}
    def tick():
        dialog = app.activeModalWidget()
        if isinstance(dialog, dialog_class):
            state["seen"] = True
            try:
                if time.monotonic() - state["started"] > timeout:
                    raise AssertionError("Modal operation timed out: " + dialog.windowTitle())
                if interact(dialog, state):
                    timer.stop()
            except BaseException as error:
                state["errors"].append(error)
                timer.stop()
                dialog.reject()
    timer.timeout.connect(tick)
    timer.start()
    try:
        trigger()
    finally:
        timer.stop()
    if state["errors"]:
        raise state["errors"][0]
    assert state["seen"], "The requested workflow did not open its real dialog"


@pytest.fixture
def http_media(tmp_path, qt_app):
    web = tmp_path / "web"
    web.mkdir()
    for index, color in enumerate(("red", "green", "blue"), 1):
        image = QImage(64 + index, 48 + index, QImage.Format.Format_RGB32)
        image.fill(QColor(color))
        assert image.save(str(web / f"photo{index}.jpg"), "JPG")
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            # A slow response lets the GUI demonstrate navigation during analysis.
            if self.path.startswith("/photo"):
                time.sleep(.05)
            return super().do_GET()
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(web)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", web
    server.shutdown(); server.server_close(); thread.join(3)


@pytest.fixture
def make_window(tmp_path, qt_app):
    widgets = []
    def create(resolver=None, engine_factory=None, **preferences):
        store = SettingsStore(tmp_path / f"data-{len(widgets)}")
        settings = Settings(localization=False, output_path=str(tmp_path / f"downloads-{len(widgets)}"), **preferences)
        store.save(settings)
        queue = QueueService(settings, store.data_dir, resolver=resolver, engine_factory=engine_factory)
        widget = MainWindow(store, queue)
        widget.quick_environment_check = lambda: None
        widget.show()
        widgets.append(widget)
        return widget
    yield create
    for widget in widgets:
        widget.close()
    qt_app.processEvents()


def analyze(qt_app, window, urls):
    window.url.setPlainText("\n".join(urls))
    window.analyze_button.click()
    window.nav[4].click()
    assert window.pages.currentIndex() == 4
    wait(qt_app, lambda: window.analysis is not None and window.analyze_button.isEnabled())
    window.nav[0].click()


def completed(qt_app, window, count):
    wait(qt_app, lambda: len(window.queue.snapshot()) == count and all(task["status"] in {"Completed", "Failed", "Cancelled"} for task in window.queue.snapshot()))
    tasks = window.queue.snapshot()
    assert all(task["status"] == "Completed" for task in tasks), [(task["title"], task["error"]) for task in tasks]
    window.poll()
    return tasks


def choose_rename(qt_app, window, template, output=None):
    def interact(dialog, state):
        if not state.get("configured"):
            dialog.template.setText(template)
            dialog.enabled.setChecked(True)
            dialog.collision.setCurrentIndex(dialog.collision.findData("append_number"))
            if output:
                dialog.output.setText(str(output))
            state["configured"] = True
            return False
        if dialog.preview_pending or not dialog.plan:
            return False
        assert dialog.apply_button.isEnabled(), dialog.status.text()
        dialog.apply_button.click()
        return True
    drive_modal(qt_app, RenameDialog, window.rename_button.click, interact)


def test_single_photo_real_analysis_download_and_predownload_rename(qt_app, make_window, http_media, tmp_path):
    base, web = http_media
    window = make_window()
    analyze(qt_app, window, [base + "/photo1.jpg"])
    assert window.kind.currentData() == "photo"
    assert window.quality.findData("original") >= 0
    assert window.container.currentData() == "original"
    assert window.analysis["width"] == 65 and window.analysis["height"] == 49
    assert window.download_button.isEnabled()
    assert window.kind.findData("video") < 0
    output = tmp_path / "organized"
    choose_rename(qt_app, window, "Фото - {number:03}", output)
    assert window.output.text() == str(output)
    assert window.rename_verified
    window.download_button.click()
    tasks = completed(qt_app, window, 1)
    downloaded = output / "Фото - 001.jpg"
    assert downloaded.read_bytes() == (web / "photo1.jpg").read_bytes()
    assert str(downloaded) in tasks[0]["files"]
    assert tasks[0]["options"]["target_path"] == str(downloaded)
    assert window.library.rowCount() == 1


def test_folder_only_download_keeps_preview_filename_when_rename_disabled(qt_app, make_window, http_media):
    base, web = http_media
    window = make_window(metadata_preserve=False)
    analyze(qt_app, window, [base + "/photo1.jpg"])
    accepted_plan = []
    def organize_only(dialog, state):
        if not state.get("configured"):
            dialog.template.setText("MUST-NOT-APPEAR - {number:03}")
            dialog.enabled.setChecked(False)
            dialog.organize.setChecked(True)
            dialog.folder_template.setText("{source}/Photos/{filename}")
            state["configured"] = True
            return False
        if dialog.preview_pending or not dialog.plan:
            return False
        assert dialog.apply_button.isEnabled(), dialog.status.text()
        accepted_plan.extend(deepcopy(dialog.plan))
        assert dialog.plan[0]["filename"] == "photo1.jpg"
        dialog.apply_button.click()
        return True
    drive_modal(qt_app, RenameDialog, window.rename_button.click, organize_only)
    assert window.rename_options["enabled"] is False
    assert window.rename_options["organization_enabled"] is True
    window.download_button.click()
    task = completed(qt_app, window, 1)[0]
    expected = Path(accepted_plan[0]["target_path"])
    assert task["files"] == [str(expected)]
    assert expected.read_bytes() == (web / "photo1.jpg").read_bytes()
    assert "MUST-NOT-APPEAR" not in expected.name


def test_metadata_predownload_preview_matches_real_json_target(qt_app, make_window, http_media):
    base, web = http_media
    window = make_window()
    analyze(qt_app, window, [base + "/photo1.jpg"])
    select(window.kind, "metadata")
    choose_rename(qt_app, window, "Metadata - {number:02}")
    assert window.rename_items()[0]["ext"] == "json"
    window.download_button.click()
    task = completed(qt_app, window, 1)[0]
    target = Path(task["options"]["target_path"])
    assert target.name == "Metadata - 01.json"
    assert task["files"] == [str(target)]
    assert json.loads(target.read_text(encoding="utf-8"))["analysis"]["media_type"] == "photo"


@pytest.mark.parametrize("kind, extension", [("subtitles", "srt"), ("thumbnail", "jpg")])
def test_subtitle_and_thumbnail_preview_match_queue_target_extensions(qt_app, make_window, http_media, kind, extension):
    analysis = {"id": "fixture", "source": "generic", "title": "Media", "url": "https://example.org/video",
                "media_type": "video", "backend": "yt-dlp", "thumbnail": "",
                "formats": [{"format_id": "video", "vcodec": "h264", "acodec": "aac", "ext": "mp4"}],
                "subtitles": {"en": [{"ext": "vtt"}]}, "automatic_captions": {}, "chapters": [], "capability_status": "supported"}
    class Engine:
        def download(self, item, options, **kwargs):
            target = Path(options["target_path"]); target.parent.mkdir(parents=True, exist_ok=True)
            if kind == "subtitles":
                target.write_text("1\n00:00:00,000 --> 00:00:01,000\nSubtitle fixture\n", encoding="utf-8")
            else:
                image = QImage(10, 10, QImage.Format.Format_RGB32); image.fill(QColor("blue"))
                assert image.save(str(target), "JPG")
            return {"files": [str(target)]}
    window = make_window(engine_factory=lambda _: Engine(), metadata_preserve=False)
    window.show_analysis(analysis)
    # The thumbnail capability is legitimate; its test transport is kept offline.
    if kind == "thumbnail":
        window.analysis["thumbnail"] = http_media[0] + "/photo1.jpg"
        window.media_analysis_shown(window.analysis)
    select(window.kind, kind)
    if kind == "subtitles":
        select(window.subtitles, "en"); select(window.subtitle_format, "srt")
    planned = []
    def inspect(dialog, state):
        if not state.get("configured"):
            dialog.template.setText("{title} - {number:02}")
            state["configured"] = True
            return False
        if dialog.preview_pending or not dialog.plan:
            return False
        assert Path(dialog.plan[0]["target_path"]).suffix == "." + extension
        planned.extend(deepcopy(dialog.plan)); dialog.apply_button.click(); return True
    drive_modal(qt_app, RenameDialog, window.rename_button.click, inspect)
    window.download_button.click()
    task = completed(qt_app, window, 1)[0]
    assert task["options"]["target_path"] == planned[0]["target_path"]
    assert task["files"] == [planned[0]["target_path"]]
    assert Path(task["files"][0]).suffix == "." + extension


def test_gallery_dialog_selection_filter_range_and_page_memory(qt_app):
    def item(number, kind):
        return {"id": str(number), "source": "fixture", "title": f"Item {number}", "media_type": kind,
                "collection_id": "album", "collection_index": number, "url": f"https://example.org/{number}"}
    first = {"title": "Mixed album", "entries": [item(1, "photo"), item(2, "video"), item(3, "photo")],
             "page": 1, "has_more": True, "next_page": 2}
    second = {"title": "Mixed album", "entries": [item(4, "audio"), item(5, "photo")], "page": 2, "has_more": False}
    dialog = GallerySelectionDialog(first)
    dialog.show()
    assert dialog.model.selection.selected_items() == []
    dialog.model.choose("all")
    assert len(dialog.model.selection.selected_items()) == 3
    dialog.model.choose("none")
    select(dialog.filter_box, "photo")
    assert dialog.model.rowCount() == 2
    dialog.model.choose("invert")
    assert {entry["id"] for entry in dialog.model.selection.selected_items()} == {"1", "3"}
    dialog.model.setData(dialog.model.index(1, 0), Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
    requested = []; dialog.page_requested.connect(requested.append)
    dialog.next.click(); assert requested == [2]
    dialog.set_page(second)
    assert not dialog.next.isEnabled()
    assert dialog.model.rowCount() == 1  # Photo filter survives pagination.
    dialog.range_start.setValue(1); dialog.range_end.setValue(1); dialog.select_range()
    dialog.previous.click(); assert requested == [2, 1]
    dialog.set_page(first)
    assert dialog.model.data(dialog.model.index(0, 0), Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    assert dialog.model.data(dialog.model.index(1, 0), Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked
    dialog.accept_button.click()
    assert dialog.result() == GallerySelectionDialog.DialogCode.Accepted
    assert [entry["id"] for entry in dialog.selected_entries] == ["1", "5"]
    dialog.close()


def test_collection_model_lazy_thumbnails_only_on_requested_rows(qt_app):
    requested = []
    items = [{"id": str(number), "source": "fixture", "media_type": "photo",
              "thumbnail": f"https://example.org/{number}.jpg", "title": str(number)} for number in range(1000)]
    model = CollectionModel(items, request_thumbnail=requested.append)
    assert model.rowCount() == 1000 and requested == []
    model.data(model.index(3, 1), Qt.ItemDataRole.DecorationRole)
    assert requested == ["https://example.org/3.jpg"]
    model.filter("audio")
    assert model.rowCount() == 0


def test_gallery_pagination_uses_background_resolver_and_retains_gui_selection(qt_app, make_window, http_media):
    base, web = http_media
    calls = []
    def page(number):
        indexes = [1, 2] if number == 1 else [3]
        return {"id": "paged-album", "title": "Paged album", "source": "fixture", "input_url": base + "/album",
                "media_type": "gallery", "capabilities": ["photo"], "capability_status": "supported",
                "page": number, "has_more": number == 1, "next_page": 2 if number == 1 else None,
                "entries": [normalize_gallery_item({"url": base + f"/photo{index}.jpg", "category": "fixture", "title": str(index)},
                                                     base + "/album", index) for index in indexes]}
    class Resolver:
        def analyze(self, url, control=None, page=1, page_size=80):
            calls.append((page, page_size, threading.get_ident()))
            time.sleep(.04)
            return globals_page(page)
    globals_page = page
    window = make_window(resolver=Resolver(), gallery_page_size=25)
    analyze(qt_app, window, [base + "/album"])
    main_thread = threading.get_ident()
    def select_across_pages(dialog, state):
        if not state.get("requested"):
            dialog.model.setData(dialog.model.index(0, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
            dialog.next.click()
            state["requested"] = True
            return False
        if dialog.analysis.get("page") != 2:
            assert not dialog.accept_button.isEnabled()
            return False
        dialog.range_start.setValue(1); dialog.range_end.setValue(1); dialog.select_range()
        dialog.accept_button.click()
        return True
    drive_modal(qt_app, GallerySelectionDialog, window.collection_button.click, select_across_pages)
    assert [entry["collection_index"] for entry in window.analysis["entries"]] == [1, 3]
    assert any(number == 2 and size == 25 for number, size, thread_id in calls)
    assert all(thread_id != main_thread for number, size, thread_id in calls)
    assert window.collection_confirmed and window.download_button.isEnabled()


def test_selected_gallery_photos_only_use_real_gallery_dl_and_final_names(qt_app, make_window, http_media):
    if not (ROOT / "tools" / "gallery-dl.exe").is_file():
        pytest.skip("Bundled gallery-dl is unavailable")
    base, web = http_media
    entries = [normalize_gallery_item({"url": base + f"/photo{number}.jpg", "category": "fixture", "id": str(number),
                                      "title": "Same title", "album": "Vacation", "width": 64 + number, "height": 48 + number},
                                     base + "/album", number) for number in range(1, 4)]
    for entry in entries:
        entry["thumbnail"] = ""
    collection = {"id": "album", "title": "Vacation", "source": "fixture", "input_url": base + "/album",
                  "entries": entries, "media_type": "gallery", "collection_type": "gallery",
                  "capabilities": ["photo", "metadata"], "capability_status": "supported", "page": 1}
    class Resolver:
        def analyze(self, url, control=None, **kwargs):
            return deepcopy(collection)
    window = make_window(resolver=Resolver(), metadata_preserve=False)
    analyze(qt_app, window, [base + "/album"])
    assert not window.download_button.isEnabled()
    def select_items(dialog, state):
        dialog.model.choose("all")
        dialog.model.setData(dialog.model.index(1, 0), Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
        dialog.accept_button.click()
        return True
    drive_modal(qt_app, GallerySelectionDialog, window.collection_button.click, select_items)
    assert [entry["collection_index"] for entry in window.analysis["entries"]] == [1, 3]
    assert window.download_button.isEnabled()
    choose_rename(qt_app, window, "Vacation - {number:03}")
    window.download_button.click()
    tasks = completed(qt_app, window, 2)
    output = Path(window.output.text())
    assert {path.name for path in output.iterdir()} == {"Vacation - 001.jpg", "Vacation - 002.jpg"}
    assert (output / "Vacation - 001.jpg").read_bytes() == (web / "photo1.jpg").read_bytes()
    assert (output / "Vacation - 002.jpg").read_bytes() == (web / "photo3.jpg").read_bytes()
    assert {task["collection_index"] for task in tasks} == {1, 3}
    assert all(task["analysis"]["backend"] == "gallery-dl" for task in tasks)


def test_multi_url_failed_first_preserves_independent_photo_results(qt_app, make_window, http_media):
    base, web = http_media
    actual = SourceResolver(Settings(localization=False))
    class Resolver:
        def analyze(self, url, control=None, **kwargs):
            if url.endswith("/unsupported"):
                raise ValueError("Unsupported fixture URL")
            return actual.analyze(url, control=control, **kwargs)
    window = make_window(resolver=Resolver(), metadata_preserve=False)
    analyze(qt_app, window, [base + "/unsupported", base + "/photo1.jpg", base + "/photo2.jpg"])
    assert len(window.analysis["entries"]) == 2
    assert len(window.analysis_errors) == 1
    assert "Ошибок ссылок: 1" in window.analysis_status.text()
    def choose(dialog, state):
        dialog.model.choose("all"); dialog.accept_button.click(); return True
    drive_modal(qt_app, GallerySelectionDialog, window.collection_button.click, choose)
    window.download_button.click()
    tasks = completed(qt_app, window, 2)
    assert {Path(task["files"][0]).read_bytes() for task in tasks} == {(web / "photo1.jpg").read_bytes(), (web / "photo2.jpg").read_bytes()}


def test_audio_first_class_real_http_ytdlp_ffmpeg_download(qt_app, make_window, http_media):
    if not all((ROOT / "tools" / name).is_file() for name in ("yt-dlp.exe", "ffmpeg.exe", "ffprobe.exe")):
        pytest.skip("Bundled yt-dlp/FFmpeg tools unavailable")
    base, web = http_media
    settings = Settings(localization=False)
    FFmpegProcessor(settings, base_dir=ROOT).run(["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "pcm_s16le", str(web / "sample.wav")])
    window = make_window(default_audio_format="mp3", audio_bitrate="192")
    analyze(qt_app, window, [base + "/sample.wav"])
    assert window.analysis["media_type"] == "audio"
    assert window.kind.currentData() == "audio"
    assert window.kind.findData("video") < 0
    select(window.container, "mp3")
    assert window.audio_bitrate.isVisible() and window.audio_bitrate.isEnabled()
    assert window.audio_bitrate.currentData() == "192"
    window.embed_cover.setChecked(False)
    choose_rename(qt_app, window, "Music - {number:02}")
    window.download_button.click()
    tasks = completed(qt_app, window, 1)
    audio = next(Path(path) for path in tasks[0]["files"] if path.endswith(".mp3"))
    assert audio.name == "Music - 01.mp3" and audio.stat().st_size > 1000
    result = subprocess.run([str(ROOT / "tools" / "ffprobe.exe"), "-v", "error", "-show_streams", "-of", "json", str(audio)],
                            capture_output=True, text=True, check=True, shell=False)
    streams = json.loads(result.stdout)["streams"]
    assert {stream["codec_type"] for stream in streams} == {"audio"}
    assert streams[0]["codec_name"] == "mp3"
    assert tasks[0]["media_type"] == "audio"


def test_unknown_original_audio_codec_rename_is_explained_without_dialog_crash(qt_app, make_window):
    window = make_window()
    window.show_analysis({"id": "unknown-audio", "title": "Audio", "media_type": "audio", "source": "fixture",
                          "url": "https://example.org/audio", "backend": "yt-dlp", "capability_status": "supported",
                          "formats": [{"format_id": "unknown", "vcodec": "none", "acodec": "unknown", "ext": "bin"}],
                          "capabilities": ["audio"], "subtitles": {}, "chapters": []})
    select(window.container, "original")
    assert window.rename_downloads() is False
    assert "Оригинальный аудиокодек не определён" in window.statusBar().currentMessage()
    assert QApplication.activeModalWidget() is None


def test_selected_flat_audio_playlist_resolves_before_preview_then_downloads_real_mp3(qt_app, make_window, http_media):
    if not all((ROOT / "tools" / name).is_file() for name in ("yt-dlp.exe", "ffmpeg.exe", "ffprobe.exe")):
        pytest.skip("Bundled audio tools unavailable")
    base, web = http_media
    processor = FFmpegProcessor(Settings(localization=False), base_dir=ROOT)
    for number in (1, 2, 3):
        processor.run(["-f", "lavfi", "-i", f"sine=frequency={300 + number * 100}:duration=0.4", str(web / f"track{number}.wav")])
    collection = {"id": "audio-album", "title": "Audio album", "source": "generic", "media_type": "playlist",
                  "input_url": base + "/album", "capabilities": ["playlist"], "capability_status": "supported_with_limitations",
                  "entries": [{"id": f"track{number}", "source": "generic", "url": base + f"/track{number}.wav",
                               "title": f"Flat {number}", "media_type": "metadata", "backend": "yt-dlp", "needs_analysis": True,
                               "formats": [], "collection_id": "audio-album", "collection": "Audio album", "collection_index": number}
                              for number in (1, 2, 3)]}
    calls = []
    actual = SourceResolver(Settings(localization=False))
    class Resolver:
        def analyze(self, url, control=None, **kwargs):
            calls.append((url, threading.get_ident()))
            return deepcopy(collection) if url.endswith("/album") else actual.analyze(url, control=control, **kwargs)
    window = make_window(resolver=Resolver(), metadata_preserve=False, gallery_max_concurrency=2)
    analyze(qt_app, window, [base + "/album"])
    def selected_only(dialog, state):
        dialog.model.choose("all")
        dialog.model.setData(dialog.model.index(1, 0), Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
        dialog.accept_button.click()
        return True
    drive_modal(qt_app, GallerySelectionDialog, window.collection_button.click, selected_only)
    accepted_plan = []
    def ready_preview(dialog, state):
        if not state.get("configured"):
            dialog.template.setText("{collection} - {number:02}")
            state["configured"] = True
            return False
        if dialog.preview_pending or not dialog.plan:
            return False
        assert all(Path(row["target_path"]).suffix == ".mp3" for row in dialog.plan)
        assert [item["media_type"] for item in dialog.items] == ["audio", "audio"]
        assert [item["collection_index"] for item in dialog.items] == [1, 3]
        accepted_plan.extend(deepcopy(dialog.plan))
        dialog.apply_button.click()
        return True
    def prepare_and_wait():
        window.rename_button.click()
        assert window.rename_preparation is not None
        assert not window.download_button.isEnabled()
        window.nav[4].click(); assert window.pages.currentIndex() == 4
        wait(qt_app, lambda: window.rename_verified)
        window.nav[0].click()
    drive_modal(qt_app, RenameDialog, prepare_and_wait, ready_preview, timeout=30)
    assert not window.queue.snapshot()
    assert {url for url, _ in calls} == {base + "/album", base + "/track1.wav", base + "/track3.wav"}
    assert all(thread_id != threading.get_ident() for _, thread_id in calls)
    window.download_button.click()
    tasks = completed(qt_app, window, 2)
    assert all(task["media_type"] == "audio" for task in tasks)
    assert [task["collection_index"] for task in tasks] == [1, 3]
    assert {task["files"][0] for task in tasks} == {row["target_path"] for row in accepted_plan}
    assert all(Path(task["files"][0]).stat().st_size > 1000 for task in tasks)


@pytest.mark.parametrize("change_url", [False, True])
def test_flat_rename_preparation_is_cancellable_and_stale_results_never_open_preview(qt_app, make_window, change_url):
    gate = threading.Event()
    started = threading.Event()
    calls = []
    class Resolver:
        def analyze(self, url, control=None, **kwargs):
            calls.append(url); started.set(); gate.wait(2)
            return {"id": url.rsplit("/", 1)[-1], "source": "fixture", "url": url, "media_type": "audio",
                    "formats": [{"format_id": "a", "vcodec": "none", "acodec": "mp3", "ext": "mp3"}]}
    window = make_window(resolver=Resolver(), gallery_max_concurrency=2)
    window.url.setPlainText("https://example.org/album")
    window.analyzed_urls = window.input_urls()
    entries = [{"id": str(index), "source": "fixture", "url": f"https://example.org/{index}", "media_type": "metadata",
                "backend": "yt-dlp", "needs_analysis": True, "formats": []} for index in range(10)]
    window.show_analysis({"id": "album", "source": "fixture", "title": "Album", "media_type": "playlist", "entries": entries})
    window.collection_confirmed = True
    window.rename_button.click()
    wait(qt_app, lambda: started.is_set())
    assert len(window.rename_preparation["pending"]) == 2
    assert len(calls) <= 2  # Selected count never becomes an unbounded request pool.
    if change_url:
        window.url.setPlainText("https://example.org/changed")
    else:
        window.rename_button.click()
    assert window.rename_preparation is None
    gate.set()
    deadline = time.monotonic() + .5
    while time.monotonic() < deadline:
        qt_app.processEvents(); time.sleep(.01)
    assert QApplication.activeModalWidget() is None
    assert not window.rename_verified
    assert not window.queue.snapshot()


def test_flat_rename_analysis_failure_keeps_selection_and_never_enqueues(qt_app, make_window):
    class Resolver:
        def analyze(self, url, **kwargs):
            raise ValueError("Track is unavailable")
    window = make_window(resolver=Resolver())
    window.url.setPlainText("https://example.org/album"); window.analyzed_urls = window.input_urls()
    entry = {"id": "track", "source": "fixture", "url": "https://example.org/track", "media_type": "metadata",
             "backend": "yt-dlp", "needs_analysis": True, "formats": []}
    window.show_analysis({"title": "Album", "media_type": "playlist", "entries": [entry]})
    window.collection_confirmed = True
    window.rename_button.click()
    wait(qt_app, lambda: window.rename_preparation is None)
    assert "Track is unavailable" in window.statusBar().currentMessage()
    assert len(window.analysis["entries"]) == 1
    assert not window.queue.snapshot()
    assert QApplication.activeModalWidget() is None


def test_media_settings_gui_roundtrip_all_new_categories(qt_app, make_window):
    window = make_window()
    for key, value in (("default_audio_format", "flac"), ("default_video_format", "mkv"),
                       ("collision_policy", "append_number"), ("auth_mode", "cookies")):
        select(window.setting_fields[key], value)
    for key, value in (("simultaneous_downloads", 3), ("gallery_page_size", 120), ("gallery_max_concurrency", 2),
                       ("rename_number_start", 7), ("rename_number_step", 2), ("rename_number_padding", 4)):
        window.setting_fields[key].setValue(value)
    window.setting_fields["gallery_dl_path"].setText(str(ROOT / "tools" / "gallery-dl.exe"))
    window.setting_fields["cookie_file"].setText(str(window.store.data_dir / "cookies.txt"))
    window.setting_fields["folder_template"].setText("{source}/{collection}")
    window.setting_fields["rename_template"].setText("Тест - {number:04}")
    window.setting_fields["folder_organization"].setChecked(True)
    window.setting_fields["thumbnail_cache"].setChecked(False)
    window.setting_fields["lazy_loading"].setChecked(True)
    window.rename_before.setChecked(True)
    window.audio_bitrate.setCurrentIndex(window.audio_bitrate.findData("256"))
    window.save_settings()
    saved = window.store.load()
    assert saved.default_audio_format == "flac" and saved.default_video_format == "mkv"
    assert saved.simultaneous_downloads == 3 and saved.gallery_page_size == 120
    assert saved.gallery_max_concurrency == 2 and not saved.thumbnail_cache
    assert saved.rename_number_start == 7 and saved.rename_number_step == 2 and saved.rename_number_padding == 4
    assert saved.rename_template == "Тест - {number:04}"
    assert saved.folder_organization and saved.rename_before_download and saved.audio_bitrate == "256"
    assert saved.auth_mode == "cookies" and saved.cookie_file.endswith("cookies.txt")
    assert saved.collision_policy == "append_number"


@pytest.mark.parametrize("content", [b'{"version":1,broken', b'[]', b'{"version":1,"presets":{"Broken":{"template":42}}}'])
def test_corrupt_editable_preset_does_not_block_launch_and_is_preserved(qt_app, make_window, tmp_path, content):
    data = tmp_path / "data-0"; data.mkdir()
    path = data / "UMD_RENAME_PRESETS.json"; path.write_bytes(content)
    window = make_window()
    qt_app.processEvents()
    assert window.isVisible()
    assert window.rename_preset.findData("Video") >= 0
    assert "Файл сохранён" in window.rename_preset_warning
    assert "Файл сохранён" in window.statusBar().currentMessage()
    window.choose_rename_preset()
    assert window.rename_options["template"] == "{title}"
    assert path.read_bytes() == content
    dialog = RenameDialog([{"title": "Photo", "ext": "jpg"}], tmp_path / "output", presets=path)
    dialog.show()
    wait(qt_app, lambda: not dialog.preview_pending and bool(dialog.plan))
    assert dialog.preset.findData("Video") >= 0
    assert "Исходный файл сохранён" in dialog.status.text()
    assert dialog.apply_button.isEnabled()
    assert path.read_bytes() == content
    dialog.reject()


def test_completed_photo_real_local_rename_updates_queue_and_history(qt_app, make_window, http_media, monkeypatch):
    base, web = http_media
    window = make_window(metadata_preserve=False)
    analyze(qt_app, window, [base + "/photo2.jpg"])
    window.download_button.click()
    task = completed(qt_app, window, 1)[0]
    old = Path(task["files"][0])
    assert old.read_bytes() == (web / "photo2.jpg").read_bytes()
    window.library.selectRow(0)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    def rename(dialog, state):
        if not state.get("configured"):
            dialog.template.setText("После загрузки - {number:03}")
            state["configured"] = True
            return False
        if dialog.preview_pending or not dialog.plan or dialog.applying:
            return False
        dialog.apply_button.click()
        # The nested loop remains alive while actual asynchronous file changes run.
        return True
    drive_modal(qt_app, RenameDialog, window.rename_completed, rename)
    renamed = old.parent / "После загрузки - 001.jpg"
    assert renamed.read_bytes() == (web / "photo2.jpg").read_bytes()
    assert not old.exists()
    updated = window.queue.snapshot()[0]
    assert updated["files"] == [str(renamed)]
    window.render_download_history()
    assert str(renamed) in window.download_history.toPlainText()
    assert str(old) not in window.download_history.toPlainText()


def test_completed_media_and_metadata_sidecar_rename_preserve_actual_extensions(qt_app, make_window, http_media, monkeypatch):
    base, web = http_media
    window = make_window(metadata_preserve=True)
    analyze(qt_app, window, [base + "/photo3.jpg"])
    window.download_button.click()
    task = completed(qt_app, window, 1)[0]
    assert len(task["files"]) == 2
    original_files = [Path(path) for path in task["files"]]
    original_json = json.loads(next(file for file in original_files if file.suffix == ".json").read_text(encoding="utf-8"))
    window.library.selectRow(0)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    def rename(dialog, state):
        if not state.get("configured"):
            dialog.template.setText("Archive - {title}")
            state["configured"] = True
            return False
        if dialog.preview_pending or not dialog.plan or dialog.applying:
            return False
        assert {Path(row["target_path"]).suffix for row in dialog.plan} == {".jpg", ".json"}
        assert all(row["status"] == "ready" for row in dialog.plan), dialog.plan
        dialog.apply_button.click()
        return True
    drive_modal(qt_app, RenameDialog, window.rename_completed, rename)
    updated = window.queue.snapshot()[0]
    targets = [Path(path) for path in updated["files"]]
    assert all(target.exists() for target in targets)
    assert all(not old.exists() for old in original_files)
    assert {target.suffix for target in targets} == {".jpg", ".json"}
    assert next(target for target in targets if target.suffix == ".jpg").read_bytes() == (web / "photo3.jpg").read_bytes()
    assert json.loads(next(target for target in targets if target.suffix == ".json").read_text(encoding="utf-8")) == original_json
    window.render_download_history()
    assert all(str(target) in window.download_history.toPlainText() for target in targets)


def test_large_queue_bounds_widgets_and_preserves_multiple_selected_tasks(qt_app, make_window, monkeypatch):
    window = make_window()
    tasks = [{"id": str(index), "url": f"https://example.org/{index}.jpg", "title": f"Photo {index}",
              "source": "test", "status": "Paused", "options": {"media_type": "photo"}, "files": []}
             for index in range(501)]
    monkeypatch.setattr(window.queue, "snapshot", lambda **kwargs: deepcopy(tasks))
    window.refresh_queue(force=True)
    assert window.queue_table.rowCount() == 200
    assert window.queue_next.isEnabled() and not window.queue_previous.isEnabled()
    for row in (2, 5):
        for column in range(window.queue_table.columnCount()):
            window.queue_table.item(row, column).setSelected(True)
    tasks[0]["progress"] = 42
    window.refresh_queue()
    assert {window.queue_table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole)
            for index in window.queue_table.selectionModel().selectedRows()} == {"2", "5"}
    actions = []
    monkeypatch.setattr(window.queue, "pause", actions.append)
    window.selected_action("pause")
    assert set(actions) == {"2", "5"}
    window.queue_next.click()
    assert window.queue_table.item(0, 0).data(Qt.ItemDataRole.UserRole) == "200"
    window.queue_next.click()
    assert window.queue_table.rowCount() == 101
    assert not window.queue_next.isEnabled()
    tasks[:] = tasks[:3]
    window.refresh_queue(force=True)
    assert window.queue_page == 0 and window.queue_table.rowCount() == 3


def test_thumbnail_decodes_large_photo_to_small_pixmap(qt_app):
    from app.ui.thumbnail import thumbnail_pixmap
    image = QImage(6000, 4000, QImage.Format.Format_RGB32)
    image.fill(QColor("navy"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    thumbnail = thumbnail_pixmap(buffer.data(), QSize(72, 44))
    assert not thumbnail.isNull()
    assert thumbnail.width() <= 72 and thumbnail.height() <= 44
    assert thumbnail_pixmap(b"invalid", QSize(72, 44)).isNull()
    assert thumbnail_pixmap(b"x" * 8_000_001, QSize(72, 44)).isNull()


def test_gallery_eager_thumbnails_are_limited_to_current_page(qt_app):
    requested = []
    entries = [{"id": str(index), "source": "test", "media_type": "photo", "thumbnail": f"https://example.org/{index}.jpg"}
               for index in range(4)]
    dialog = GallerySelectionDialog({"entries": entries, "page": 1, "has_more": True},
                                    request_thumbnail=requested.append, lazy_loading=False)
    try:
        dialog.refresh_thumbnails()
        assert requested == [item["thumbnail"] for item in entries]
        requested.clear()
        dialog.thumbnail_cache[entries[0]["thumbnail"]] = True
        dialog.refresh_thumbnails()
        assert entries[0]["thumbnail"] not in requested
        assert len(requested) == 3
    finally:
        dialog.close()
