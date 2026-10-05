from copy import deepcopy
import json
from pathlib import Path
import threading
import time

import pytest

from app.core.config import Settings
from app.core.errors import ConfigurationError, TaskBusyError
from app.tasks.queue import ACTIVE, QueueService


def item(identifier, kind="photo"):
    return {"id": str(identifier), "source": "fixture", "url": f"https://example.org/{identifier}.jpg",
            "title": f"Photo {identifier}", "media_type": kind, "backend": "gallery-dl",
            "ext": "jpg", "formats": [{"format_id": "original", "ext": "jpg"}]}


def wait(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("Queue did not reach the expected state")


class Engine:
    def __init__(self, blocking=False):
        self.release = threading.Event()
        self.blocking = blocking
        self.mutex = threading.Lock()
        self.running = 0
        self.peak = 0
        self.calls = []

    def download(self, analysis, options, on_progress=None, control=None):
        with self.mutex:
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.calls.append((analysis["id"], deepcopy(options)))
        try:
            while self.blocking and not self.release.wait(.01):
                if control.pause.is_set() or control.cancel.is_set():
                    error = RuntimeError("Interrupted")
                    error.reason = "cancelled" if control.cancel.is_set() else "paused"
                    raise error
            output = Path(options.get("target_path") or Path(options["output_path"]) / f"{analysis['id']}.jpg")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"photo fixture")
            return {"files": [str(output)], "metadata": {"media_type": analysis.get("media_type")}}
        finally:
            with self.mutex:
                self.running -= 1


@pytest.fixture
def factory(tmp_path):
    queues = []
    def create(engine=None, resolver=None, **values):
        engine = engine or Engine()
        settings = Settings(output_path=str(tmp_path / "output"), **values)
        queue = QueueService(settings, tmp_path, resolver=resolver, engine_factory=lambda _: engine)
        queues.append(queue)
        return queue, engine
    yield create
    for queue in queues:
        queue.shutdown(2)


def test_only_selected_gallery_items_are_queued_with_collection_context(factory):
    queue, engine = factory()
    collection = {"id": "album", "collection_id": "album", "title": "Album", "source": "fixture",
                  "entries": [item(1), item(2), item(3)]}
    tasks = queue.enqueue(collection, {"media_type": "auto"}, selected_ids=["fixture:1", "3"])
    wait(lambda: all(task["status"] == "Completed" for task in queue.snapshot()))
    assert [task["media_id"] for task in tasks] == ["1", "3"]
    assert all(task["media_type"] == "photo" and task["collection_id"] == "album" for task in tasks)
    assert [options["quality"] for _, options in engine.calls] == ["original", "original"]
    assert queue.enqueue(collection, selected_ids=[]) == []


def test_auto_mixed_collection_uses_actual_kinds_and_saved_format_preferences(factory):
    queue, _ = factory(default_audio_format="flac", default_video_format="mkv")
    entries = [item(1, "photo"), item(2, "audio"), item(3, "video")]
    for entry in entries[1:]:
        entry["backend"] = "yt-dlp"
    tasks = queue.enqueue({"entries": entries}, {"media_type": "auto"})
    assert [(task["media_type"], task["options"]["container"]) for task in tasks] == [("photo", "original"), ("audio", "flac"), ("video", "mkv")]
    assert all(task["settings"]["simultaneous_downloads"] == 1 for task in tasks)


def test_analysis_pagination_passes_cancellation_and_explicit_bounds(factory):
    class Resolver:
        def analyze(self, url, control=None, page=1, page_size=80):
            return {"page": page, "page_size": page_size, "control_received": control is not None, "entries": [item(page)]}
    queue, _ = factory(resolver=Resolver())
    request = queue.analyze("https://example.org/album", page=3, page_size=25)
    events = []
    wait(lambda: events.extend(queue.poll_events()) or any(event["type"] == "analysis_ready" for event in events))
    result = next(event for event in events if event["type"] == "analysis_ready" and event["request_id"] == request)["analysis"]
    assert result["page"] == 3 and result["page_size"] == 25 and result["control_received"]
    with pytest.raises(ConfigurationError):
        queue.analyze("url", page_size=10000)


def test_download_pool_is_bounded_and_all_running_tasks_pause_resume(factory):
    engine = Engine(blocking=True)
    queue, _ = factory(engine=engine, simultaneous_downloads=3)
    queue.enqueue({"entries": [item(index) for index in range(6)]}, {"media_type": "auto"})
    wait(lambda: engine.running == 3)
    assert engine.peak == 3
    queue.pause_all()
    wait(lambda: engine.running == 0)
    assert all(task["status"] == "Paused" for task in queue.snapshot())
    queue.resume_all()
    wait(lambda: engine.running == 3)
    engine.release.set()
    wait(lambda: all(task["status"] == "Completed" for task in queue.snapshot()))
    assert engine.peak == 3


def test_process_lock_is_shared_by_workers_and_released_only_after_shutdown(factory, tmp_path):
    queue, engine = factory(engine=Engine(blocking=True), simultaneous_downloads=2)
    queue.enqueue({"entries": [item(1), item(2)]}, {"media_type": "auto"})
    wait(lambda: engine.running == 2)
    with pytest.raises(TaskBusyError):
        QueueService(Settings(output_path=str(tmp_path / "output")), tmp_path)
    assert queue.shutdown(2)
    assert engine.running == 0
    reopened = QueueService(Settings(output_path=str(tmp_path / "output")), tmp_path, engine_factory=lambda _: Engine())
    try:
        assert all(task["status"] == "Paused" for task in reopened.snapshot())
    finally:
        reopened.shutdown(2)


def test_confirmed_local_rename_updates_persisted_history_paths(factory, tmp_path):
    queue, _ = factory()
    queue.enqueue(item(1), {"media_type": "auto"})
    wait(lambda: queue.snapshot()[0]["status"] == "Completed")
    original = queue.snapshot()[0]["files"][0]
    target = str(tmp_path / "output" / "Renamed.jpg")
    Path(original).rename(target)
    queue.update_file_paths({original: target})
    assert queue.snapshot()[0]["files"] == [target]
    assert json.loads(queue.path.read_text(encoding="utf-8"))["tasks"][0]["files"] == [target]


def test_whole_batch_rename_plans_extensions_numbering_and_prevents_duplicate_requests(factory):
    pytest.importorskip("app.rename.service")
    queue, engine = factory()
    options = {"media_type": "auto", "rename": {"enabled": True, "template": "旅行 - {number:03}", "collision_policy": "append_number"}}
    tasks = queue.enqueue({"entries": [item(1), item(2)]}, options)
    assert [Path(task["options"]["target_path"]).name for task in tasks] == ["旅行 - 001.jpg", "旅行 - 002.jpg"]
    wait(lambda: all(task["status"] == "Completed" for task in queue.snapshot()))
    assert all(Path(task["files"][0]).exists() for task in queue.snapshot())
    assert queue.enqueue({"entries": [item(1), item(2)]}, options) == []


def test_ask_conflict_rejects_entire_batch_before_any_download(factory, tmp_path):
    pytest.importorskip("app.rename.service")
    queue, engine = factory()
    output = tmp_path / "output"
    output.mkdir(exist_ok=True)
    (output / "Photo 1.jpg").write_bytes(b"User file")
    with pytest.raises(ConfigurationError, match="конфликт"):
        queue.enqueue({"entries": [item(1), item(2)]}, {"media_type": "auto", "rename": {"enabled": True, "template": "{title}", "collision_policy": "ask"}})
    assert queue.snapshot() == [] and engine.calls == []
    assert (output / "Photo 1.jpg").read_bytes() == b"User file"


def test_parallel_analysis_does_not_let_one_slow_url_block_other_requests(factory):
    release = threading.Event()
    started = threading.Event()
    class Resolver:
        def analyze(self, url, control=None, **kwargs):
            if url == "slow":
                started.set()
                while not release.wait(.01):
                    if control.cancel.is_set():
                        return {}
            return {"url": url, "entries": [item(url)]}
    queue, _ = factory(resolver=Resolver(), gallery_max_concurrency=2)
    queue.analyze("slow")
    assert started.wait(2)
    second = queue.analyze("fast")
    events = []
    try:
        wait(lambda: events.extend(queue.poll_events()) or any(event["type"] == "analysis_ready" and event["request_id"] == second for event in events))
    finally:
        release.set()


@pytest.mark.parametrize("status", sorted(ACTIVE))
def test_all_extended_active_states_recover_as_paused_with_collection_preferences(factory, tmp_path, status):
    queue, _ = factory()
    queue.enqueue({"collection_id": "album", "entries": [item(1)]}, {"media_type": "auto"})
    wait(lambda: queue.snapshot()[0]["status"] == "Completed")
    assert queue.shutdown(2)
    stored = json.loads(queue.path.read_text(encoding="utf-8"))
    stored["tasks"][0]["status"] = status
    queue.path.write_text(json.dumps(stored), encoding="utf-8")
    reopened, _ = factory()
    recovered = reopened.snapshot()[0]
    assert recovered["status"] == "Paused"
    assert recovered["media_type"] == "photo" and recovered["collection_id"] == "album"
    assert recovered["options"]["metadata_preserve"] and recovered["options"]["embed_cover"]


def test_rename_planner_reserves_targets_of_other_running_batches(factory):
    pytest.importorskip("app.rename.service")
    queue, engine = factory(engine=Engine(blocking=True), simultaneous_downloads=2)
    options = {"media_type": "auto", "rename": {"enabled": True, "template": "Same", "collision_policy": "append_number"}}
    first = queue.enqueue(item(1), options)
    second = queue.enqueue(item(2), options)
    assert Path(first[0]["options"]["target_path"]).name == "Same.jpg"
    assert Path(second[0]["options"]["target_path"]).name == "Same (1).jpg"
    engine.release.set()
    wait(lambda: all(task["status"] == "Completed" for task in queue.snapshot()))


def test_native_audio_defaults_and_photo_metadata_mode(factory):
    queue, _ = factory(default_audio_format="flac")
    audio = queue.enqueue({**item(1, "audio"), "backend": "yt-dlp"})[0]
    photo_metadata = queue.enqueue(item(2), {"media_type": "metadata"})[0]
    assert audio["options"]["media_type"] == "audio" and audio["options"]["container"] == "flac"
    assert photo_metadata["options"]["media_type"] == "metadata"


def test_native_gallery_video_retains_original_extension_without_claiming_transcode(factory):
    queue, _ = factory(default_video_format="mkv")
    video = {**item(1, "video"), "ext": "webm", "formats": [{"format_id": "original", "ext": "webm"}]}
    task = queue.enqueue(video, {"media_type": "auto", "rename": {"enabled": True, "template": "{title}", "collision_policy": "ask"}})[0]
    assert task["options"]["container"] == "original"
    assert Path(task["options"]["target_path"]).suffix == ".webm"


def test_default_engine_uses_router_with_analysis_before_transfer(monkeypatch, tmp_path):
    from app.downloader import router
    engine = Engine()
    calls = []
    def routed(settings, analysis, base_dir=None):
        calls.append((settings, analysis))
        return engine
    monkeypatch.setattr(router, "create_downloader", routed)
    queue = QueueService(Settings(output_path=str(tmp_path / "output")), tmp_path)
    try:
        queue.enqueue(item(1))
        wait(lambda: queue.snapshot()[0]["status"] == "Completed")
        assert calls[0][1]["backend"] == "gallery-dl"
    finally:
        queue.shutdown(2)


def test_original_format_without_extension_cannot_produce_a_fabricated_rename_preview(factory):
    queue, engine = factory()
    unresolved = {"id": "flat", "source": "youtube", "url": "https://example.org/watch",
                  "title": "Unresolved entry", "media_type": "video", "needs_analysis": True}
    with pytest.raises(ConfigurationError, match="расширение"):
        queue.enqueue(unresolved, {"container": "original", "rename": {"enabled": True, "template": "{title}"}})
    assert queue.snapshot() == [] and engine.calls == []


@pytest.mark.parametrize("container,codec,extension", [("mp4", "mp4a.40.2", "m4a"),
                                                       ("webm", "opus", "opus"),
                                                       ("webm", "vorbis", "ogg")])
def test_original_audio_rename_matches_actual_extracted_codec_for_gui_and_queue(factory, container, codec, extension):
    from types import SimpleNamespace
    from app.ui.media_expansion import MediaExpansionMixin
    media = {"id": "track", "source": "fixture", "backend": "yt-dlp", "media_type": "video",
             "url": "https://example.org/track", "title": "Native audio", "ext": container,
             "formats": [{"format_id": "combined", "ext": container, "vcodec": "vp9", "acodec": codec, "abr": 128}]}
    queue, _ = factory()
    options = {"media_type": "audio", "container": "original", "rename": {"enabled": True, "template": "{title}", "collision_policy": "ask"}}
    task = queue.enqueue(media, options)[0]
    control = lambda value: SimpleNamespace(currentData=lambda: value)
    gui = SimpleNamespace(analysis=media, container=control("original"), kind=control("audio"),
                          audio=control("with_audio"), quality=control("best"), settings=queue.settings)
    preview = MediaExpansionMixin.rename_items(gui)
    assert preview[0]["ext"] == extension
    assert Path(task["options"]["target_path"]).suffix == "." + extension
    assert media["ext"] == container  # Preview never mutates source metadata.


def test_original_audio_rename_uses_selected_format_and_prefers_native_audio_only(factory):
    media = {"id": "formats", "source": "fixture", "backend": "yt-dlp", "media_type": "audio",
             "url": "https://example.org/audio", "title": "Audio",
             "formats": [{"format_id": "combined", "ext": "mp4", "vcodec": "h264", "acodec": "aac", "abr": 320},
                         {"format_id": "native", "ext": "webm", "vcodec": "none", "acodec": "opus", "abr": 128}]}
    queue, _ = factory(engine=Engine(blocking=True))
    options = {"media_type": "audio", "container": "original", "rename": {"enabled": True, "template": "{title}", "collision_policy": "append_number"}}
    best = queue.enqueue(media, options)[0]
    explicit = queue.enqueue(media, {**options, "quality": "format:combined"})[0]
    assert Path(best["options"]["target_path"]).suffix == ".opus"
    assert Path(explicit["options"]["target_path"]).suffix == ".m4a"


def test_unknown_original_audio_codec_never_uses_source_video_container_as_preview(factory):
    media = {"id": "unknown", "source": "fixture", "backend": "yt-dlp", "media_type": "video",
             "url": "https://example.org/audio", "title": "Unknown",
             "formats": [{"format_id": "unknown", "ext": "mp4", "vcodec": "h264", "acodec": "unknown"}]}
    queue, engine = factory()
    with pytest.raises(ConfigurationError, match="расширение"):
        queue.enqueue(media, {"media_type": "audio", "container": "original", "rename": {"enabled": True, "template": "{title}"}})
    assert queue.snapshot() == [] and engine.calls == []


def test_unresolved_original_audio_without_rename_still_queues_background_analysis(factory):
    class Resolver:
        def analyze(self, url):
            return {"id": "pending", "source": "fixture", "url": url, "media_type": "audio", "backend": "yt-dlp",
                    "formats": [{"format_id": "audio", "ext": "m4a", "vcodec": "none", "acodec": "aac"}]}
    queue, _ = factory(resolver=Resolver())
    unresolved = {"id": "pending", "source": "fixture", "url": "https://example.org/audio", "media_type": "audio", "backend": "yt-dlp", "needs_analysis": True}
    queue.enqueue(unresolved, {"media_type": "audio", "container": "original"})
    wait(lambda: queue.snapshot()[0]["status"] == "Completed")
    assert not queue.snapshot()[0]["needs_analysis"]


def flat_audio():
    return {"id": "flat-track", "source": "soundcloud", "url": "https://example.org/track",
            "title": "Flat track", "backend": "yt-dlp", "media_type": "metadata",
            "formats": [], "needs_analysis": True, "collection_index": 7}


class ActualAudioResolver:
    def analyze(self, url, control=None):
        return {"id": "flat-track", "source": "soundcloud", "url": url, "title": "Actual track",
                "backend": "yt-dlp", "media_type": "audio", "needs_analysis": False,
                "formats": [{"format_id": "audio", "ext": "m4a", "vcodec": "none", "acodec": "aac"}]}


class AudioOnlyEngine(Engine):
    def download(self, analysis, options, **kwargs):
        assert analysis["media_type"] == "audio"
        assert options["media_type"] == "audio" and options["container"] == "flac"
        assert options["quality"] == "best"
        return super().download(analysis, options, **kwargs)


def test_auto_flat_playlist_resolves_actual_audio_and_uses_saved_audio_preferences(factory):
    queue, engine = factory(engine=AudioOnlyEngine(), resolver=ActualAudioResolver(), default_audio_format="flac")
    tasks = queue.enqueue({"id": "album", "collection_id": "album", "media_type": "playlist", "entries": [flat_audio()]}, {"media_type": "auto"})
    assert tasks[0]["options"]["media_type"] == "auto"
    assert tasks[0]["requested_options"]["media_type"] == "auto"
    wait(lambda: queue.snapshot()[0]["status"] == "Completed")
    resolved = queue.snapshot()[0]
    assert engine.calls[0][1]["container"] == "flac"
    assert resolved["media_type"] == "audio"
    assert resolved["collection_id"] == "album" and resolved["collection_index"] == 7
    assert resolved["analysis"]["collection_index"] == 7
    stored = json.loads(queue.path.read_text(encoding="utf-8"))["tasks"][0]
    assert stored["options"]["media_type"] == "audio" and stored["requested_options"]["media_type"] == "auto"


def test_unresolved_auto_survives_restart_and_then_downloads_actual_audio(factory):
    started = threading.Event()
    class SlowResolver:
        def analyze(self, url, control=None):
            started.set()
            while not control.pause.wait(.01):
                pass
            return {}
    queue, _ = factory(engine=AudioOnlyEngine(), resolver=SlowResolver(), default_audio_format="flac")
    queue.enqueue({"media_type": "playlist", "entries": [flat_audio()]}, {"media_type": "auto"})
    assert started.wait(2)
    assert queue.shutdown(2)
    pending = json.loads(queue.path.read_text(encoding="utf-8"))["tasks"][0]
    assert pending["status"] == "Paused" and pending["needs_analysis"]
    assert pending["options"]["media_type"] == "auto" and pending["requested_options"]["media_type"] == "auto"
    reopened, engine = factory(engine=AudioOnlyEngine(), resolver=ActualAudioResolver(), default_audio_format="mp3")
    reopened.resume_all()
    wait(lambda: reopened.snapshot()[0]["status"] == "Completed")
    assert engine.calls[0][1]["container"] == "flac"  # Saved request preferences survive UI changes.


def test_unknown_auto_flat_entry_cannot_fabricate_mp4_pre_download_rename(factory):
    queue, engine = factory()
    with pytest.raises(ConfigurationError, match="Тип элементов.*неизвестен"):
        queue.enqueue({"media_type": "playlist", "entries": [flat_audio()]},
                      {"media_type": "auto", "rename": {"enabled": True, "template": "{title}"}})
    assert queue.snapshot() == [] and engine.calls == []


def test_audio_hint_still_refreshes_auto_options_after_actual_reanalysis(factory):
    queue, engine = factory(engine=AudioOnlyEngine(), resolver=ActualAudioResolver(), default_audio_format="flac")
    hinted = {**flat_audio(), "media_type": "audio"}
    queue.enqueue({"media_type": "playlist", "entries": [hinted]}, {"media_type": "auto"})
    wait(lambda: queue.snapshot()[0]["status"] == "Completed")
    assert queue.snapshot()[0]["media_type"] == "audio" and engine.calls[0][1]["media_type"] == "audio"
