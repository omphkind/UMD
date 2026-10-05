from copy import deepcopy
import json
from pathlib import Path
import threading
import time

import pytest

from app.core.config import Settings, SettingsStore
from app.core.errors import ConfigurationError, StorageError, TaskBusyError
from app.tasks.queue import QueueService


def analysis(media_id="aaaaaaaaaaa", **values):
    return {"source": "youtube", "id": media_id,
            "url": f"https://www.youtube.com/watch?v={media_id}",
            "title": "Test title", "thumbnail": "https://example.com/thumb.jpg",
            "formats": [{"format_id": "1", "height": 1080}], **values}


def wait_for(check, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        threading.Event().wait(0.01)
    raise AssertionError("Background operation did not complete")


class Interrupted(Exception):
    def __init__(self, reason):
        self.reason = reason


class FakeEngine:
    def __init__(self, *, block=False, fail_ids=None):
        self.calls = []
        self.options = []
        self.block = block
        self.release = threading.Event()
        self.fail_ids = fail_ids or set()

    def download(self, item, options, on_progress=None, control=None):
        self.calls.append(item["id"])
        self.options.append(deepcopy(options))
        if item["id"] in self.fail_ids:
            raise RuntimeError("Fixture network failure")
        on_progress({"stage": "Downloading", "progress": 30.0, "speed": 1024.0, "eta": 7.0})
        while self.block and not self.release.wait(0.01):
            if control.cancel.is_set():
                raise Interrupted("cancelled")
            if control.pause.is_set():
                raise Interrupted("paused")
        on_progress({"stage": "Processing", "progress": 90.0})
        output = Path(options["output_path"])
        output.mkdir(parents=True, exist_ok=True)
        file = output / f"{item['id']}.mp4"
        file.write_bytes(b"fixture media")
        return {"files": [str(file)], "metadata": {"title": item["title"]}}


class FakeResolver:
    def __init__(self):
        self.calls = []
        self.fail = False

    def analyze(self, url):
        self.calls.append(url)
        if self.fail:
            raise ConfigurationError("Fixture unsupported URL")
        return analysis(url.partition("v=")[2] or "aaaaaaaaaaa")


@pytest.fixture
def queue_factory(tmp_path):
    queues = []

    def factory(engine=None, resolver=None, settings=None):
        engine = engine or FakeEngine()
        queue = QueueService(settings or Settings(localization=False, output_path=str(tmp_path / "downloads")), tmp_path,
                             resolver=resolver or FakeResolver(), engine_factory=lambda _: engine)
        queues.append(queue)
        return queue, engine

    yield factory
    for queue in queues:
        queue.shutdown(1)


def test_async_analysis_events_success_and_friendly_error(queue_factory):
    resolver = FakeResolver()
    queue, _ = queue_factory(resolver=resolver)
    request_id = queue.analyze("https://www.youtube.com/watch?v=aaaaaaaaaaa")
    events = []
    wait_for(lambda: events.extend(queue.poll_events()) or any(event["type"] == "analysis_ready" for event in events))
    ready = next(event for event in events if event["type"] == "analysis_ready")
    assert ready["request_id"] == request_id
    assert ready["analysis"]["formats"][0]["height"] == 1080
    resolver.fail = True
    queue.analyze("bad")
    wait_for(lambda: events.extend(queue.poll_events()) or any(event["type"] == "analysis_failed" for event in events))
    assert "unsupported URL" in next(event for event in events if event["type"] == "analysis_failed")["error"]


def test_completion_durable_files_stage_progress_and_detached_snapshot(queue_factory, tmp_path):
    queue, engine = queue_factory()
    added = queue.enqueue(analysis())
    wait_for(lambda: queue.snapshot()[0]["status"] == "Completed")
    assert engine.calls == ["aaaaaaaaaaa"]
    task = queue.snapshot()[0]
    assert task["progress"] == 100
    assert Path(task["files"][0]).read_bytes() == b"fixture media"
    stored = json.loads((tmp_path / "UMD_QUEUE.json").read_text(encoding="utf-8"))
    assert stored["tasks"][0]["status"] == "Completed"
    task["options"]["quality"] = "wrong"
    assert queue.snapshot()[0]["options"]["quality"] == "best"
    events = queue.poll_events()
    assert any(event["type"] == "progress" and event["speed"] == 1024 for event in events)
    assert any(event["type"] == "task_changed" and event["task"]["status"] == "Processing" for event in events)
    assert added[0]["status"] == "Queued"


def test_queue_deduplicates_identity_aliases_but_allows_another_quality(queue_factory):
    queue, engine = queue_factory(FakeEngine(block=True))
    first = queue.enqueue(analysis())
    assert queue.enqueue(analysis(url="https://youtu.be/aaaaaaaaaaa")) == []
    second = queue.enqueue(analysis(), {"quality": "720"})
    assert len(second) == 1 and first[0]["id"] != second[0]["id"]
    assert len(queue.snapshot()) == 2


def test_pause_resume_cancel_individual_and_all(queue_factory):
    queue, engine = queue_factory(FakeEngine(block=True))
    first = queue.enqueue(analysis())[0]["id"]
    wait_for(lambda: len(engine.calls) == 1)
    queue.pause(first)
    assert queue.snapshot()[0]["status"] == "Paused"
    # Resume immediately, even while the interrupted subprocess is exiting.
    queue.resume(first)
    wait_for(lambda: len(engine.calls) == 2)
    second = queue.enqueue(analysis("bbbbbbbbbbb"))[0]["id"]
    queue.pause_all()
    assert {task["status"] for task in queue.snapshot()} == {"Paused"}
    queue.resume_all()
    wait_for(lambda: len(engine.calls) == 3)
    queue.cancel(first)
    wait_for(lambda: "bbbbbbbbbbb" in engine.calls)
    assert queue.snapshot()[0]["status"] == "Cancelled"
    queue.cancel_all()
    assert {task["status"] for task in queue.snapshot()} == {"Cancelled"}


def test_failure_does_not_stop_queue_retry_failed_preserves_identity(queue_factory):
    queue, engine = queue_factory(FakeEngine(fail_ids={"aaaaaaaaaaa"}))
    first = queue.enqueue(analysis())[0]["id"]
    queue.enqueue(analysis("bbbbbbbbbbb"))
    wait_for(lambda: {task["status"] for task in queue.snapshot()} == {"Failed", "Completed"})
    engine.fail_ids.clear()
    queue.retry_failed()
    wait_for(lambda: all(task["status"] == "Completed" for task in queue.snapshot()))
    assert queue.snapshot()[0]["id"] == first
    assert engine.calls.count("bbbbbbbbbbb") == 1
    assert engine.calls.count("aaaaaaaaaaa") == 2
    queue.clear_completed()
    assert queue.snapshot() == []


def test_invalid_engine_metadata_does_not_corrupt_queue_or_stop_next_item(queue_factory):
    class Engine(FakeEngine):
        def download(self, item, *args, **kwargs):
            if item["id"] == "aaaaaaaaaaa":
                return {"files": [], "metadata": {"invalid": float("nan")}}
            return super().download(item, *args, **kwargs)

    queue, _ = queue_factory(engine=Engine())
    queue.enqueue(analysis())
    queue.enqueue(analysis("bbbbbbbbbbb"))
    wait_for(lambda: {task["status"] for task in queue.snapshot()} == {"Failed", "Completed"})
    assert len(json.loads(queue.path.read_text(encoding="utf-8"))["tasks"]) == 2


def test_playlist_entries_are_resolved_and_options_snapshotted(queue_factory):
    resolver = FakeResolver()
    queue, engine = queue_factory(resolver=resolver)
    playlist = {"source": "youtube", "entries": [analysis("aaaaaaaaaaa", formats=[]), analysis("bbbbbbbbbbb", formats=[])]}
    queue.enqueue(playlist, {"quality": "1080", "thumbnail": True})
    queue.settings.download_quality = "720"
    wait_for(lambda: len(engine.calls) == 2 and all(task["status"] == "Completed" for task in queue.snapshot()))
    assert len(resolver.calls) == 2
    assert all(options["quality"] == "1080" and options["thumbnail"] for options in engine.options)


@pytest.mark.parametrize("reanalyze", [False, True])
def test_generic_html_playlist_selects_original_entry_not_container(queue_factory, reanalyze):
    container_url = "https://example.com/embedded-videos"
    entries = [analysis("html-entry-1", source="generic", url=container_url,
                        playlist_url=container_url, playlist_index=1),
               analysis("html-entry-2", source="generic", url=container_url,
                        playlist_url=container_url, playlist_index=2)]
    playlist = {"source": "generic", "id": "html-container", "url": container_url,
                "media_type": "playlist", "entries": entries}

    class Resolver:
        def __init__(self):
            self.calls = []

        def analyze(self, url):
            self.calls.append(url)
            return deepcopy(playlist)

    class Engine(FakeEngine):
        def __init__(self):
            super().__init__()
            self.selected = []

        def download(self, item, *args, **kwargs):
            assert not item.get("entries")
            assert item.get("media_type") != "playlist"
            self.selected.append((item["id"], item["playlist_url"], item["playlist_index"]))
            return super().download(item, *args, **kwargs)

    resolver, engine = Resolver(), Engine()
    queue, _ = queue_factory(engine=engine, resolver=resolver)
    queued_playlist = deepcopy(playlist)
    if reanalyze:
        for entry in queued_playlist["entries"]:
            entry["formats"] = []
            entry["needs_analysis"] = True
    queue.enqueue(queued_playlist)
    wait_for(lambda: all(task["status"] == "Completed" for task in queue.snapshot()))
    assert engine.selected == [("html-entry-1", container_url, 1), ("html-entry-2", container_url, 2)]
    assert len(resolver.calls) == (2 if reanalyze else 0)


def test_unresolved_extra_url_expands_playlist_using_saved_settings(queue_factory, tmp_path):
    entered, released = threading.Event(), threading.Event()
    playlist = {"source": "generic", "url": "https://example.com/list", "media_type": "playlist",
                "entries": [analysis("child-1", source="generic", is_shorts=True),
                            analysis("child-2", source="generic")]}

    class Resolver:
        def analyze(self, url):
            entered.set()
            assert released.wait(2)
            return deepcopy(playlist)

    settings = Settings(include_shorts=True, output_path=str(tmp_path / "saved-output"))
    queue, engine = queue_factory(settings=settings, resolver=Resolver())
    placeholder = queue.enqueue({"source": "unresolved", "id": playlist["url"],
                                 "url": playlist["url"], "needs_analysis": True}, {"quality": "720"})[0]
    assert entered.wait(1)
    queue.settings.include_shorts = False
    queue.settings.download_quality = "best"
    queue.settings.output_path = str(tmp_path / "new-output")
    released.set()
    wait_for(lambda: len(queue.snapshot()) == 3 and all(task["status"] in {"Cancelled", "Completed"} for task in queue.snapshot()))
    parent = next(task for task in queue.snapshot() if task["id"] == placeholder["id"])
    assert parent["status"] == "Cancelled" and parent["skip_reason"] == "playlist_expanded"
    assert parent["error"] is None
    children = [task for task in queue.snapshot() if task["id"] != parent["id"]]
    assert len(children) == 2
    assert all(task["settings"]["include_shorts"] is True and task["options"]["quality"] == "720" for task in children)
    assert all(Path(task["files"][0]).parent == tmp_path / "saved-output" for task in children)
    assert engine.calls == ["child-1", "child-2"]


def test_unresolved_aliases_share_one_real_download_identity(queue_factory):
    class Resolver:
        def analyze(self, url):
            return analysis("aaaaaaaaaaa")

    queue, engine = queue_factory(resolver=Resolver())
    queue.enqueue(analysis("placeholder-1", source="unknown", url="https://youtu.be/aaaaaaaaaaa", needs_analysis=True))
    queue.enqueue(analysis("placeholder-2", source="unknown", url="https://www.youtube.com/watch?v=aaaaaaaaaaa", needs_analysis=True))
    wait_for(lambda: all(task["status"] in {"Completed", "Cancelled"} for task in queue.snapshot()))
    assert engine.calls == ["aaaaaaaaaaa"]
    assert {task["source"] for task in queue.snapshot()} == {"youtube"}
    duplicate = next(task for task in queue.snapshot() if task["status"] == "Cancelled")
    assert duplicate["skip_reason"] == "duplicate"
    assert duplicate["error"] is None


@pytest.mark.parametrize("flag, reason", [("is_shorts", "shorts"), ("is_members_only", "members_only")])
def test_restrictions_detected_during_analysis_are_skips_not_failures(queue_factory, flag, reason):
    class Resolver:
        def analyze(self, url):
            return analysis(**{flag: True})

    queue, engine = queue_factory(resolver=Resolver())
    queue.enqueue(analysis(needs_analysis=True))
    wait_for(lambda: queue.snapshot()[0]["status"] == "Cancelled")
    assert engine.calls == []
    assert queue.snapshot()[0]["error"] is None
    assert queue.snapshot()[0]["skip_reason"] == reason
    assert any(event["type"] == "task_skipped" and event["reason"] == reason for event in queue.poll_events())


def test_engine_expected_skip_does_not_fail_queue(queue_factory):
    from app.core.errors import SkipItem

    class Engine:
        def download(self, *args, **kwargs):
            raise SkipItem("members_only")

    queue, _ = queue_factory(engine=Engine())
    queue.enqueue(analysis())
    wait_for(lambda: queue.snapshot()[0]["status"] == "Cancelled")
    assert queue.snapshot()[0]["skip_reason"] == "members_only"
    assert queue.snapshot()[0]["error"] is None


def test_analyzer_that_has_not_returned_does_not_block_shutdown(tmp_path):
    released = threading.Event()
    started = threading.Event()

    class Resolver:
        def analyze(self, url):
            started.set()
            released.wait(5)
            return analysis()

    queue = QueueService(Settings(), tmp_path, resolver=Resolver(), engine_factory=lambda _: FakeEngine())
    try:
        queue.analyze("https://example.com/video")
        assert started.wait(1)
        clock = time.monotonic()
        assert not queue.shutdown(0.05)
        assert time.monotonic() - clock < 0.3
    finally:
        released.set()
        assert queue.shutdown(1)


def test_standalone_analysis_receives_cancellation_on_shutdown(tmp_path):
    started = threading.Event()
    cancelled = threading.Event()

    class Resolver:
        def analyze(self, url, control=None):
            assert control is not None
            started.set()
            assert control.cancel.wait(2)
            cancelled.set()
            raise Interrupted("cancelled")

    queue = QueueService(Settings(), tmp_path, resolver=Resolver(), engine_factory=lambda _: FakeEngine())
    try:
        queue.analyze("https://example.com/video")
        assert started.wait(1)
        assert queue.shutdown(1)
        assert cancelled.is_set()
        assert not any(event["type"] == "analysis_ready" for event in queue.poll_events())
    finally:
        queue.shutdown(1)


def test_shutdown_is_bounded_and_reopening_requires_resume(tmp_path):
    engine = FakeEngine(block=True)
    settings = Settings(output_path=str(tmp_path / "output"))
    queue = QueueService(settings, tmp_path, resolver=FakeResolver(), engine_factory=lambda _: engine)
    try:
        queue.enqueue(analysis())
        wait_for(lambda: bool(engine.calls))
        started = time.monotonic()
        assert queue.shutdown(1)
        assert time.monotonic() - started < 1.1
        assert json.loads(queue.path.read_text(encoding="utf-8"))["tasks"][0]["status"] == "Paused"
    finally:
        queue.shutdown(1)
    replacement = FakeEngine()
    reopened = QueueService(settings, tmp_path, resolver=FakeResolver(), engine_factory=lambda _: replacement)
    try:
        assert reopened.snapshot()[0]["status"] == "Paused"
        assert replacement.calls == []
        reopened.resume_all()
        wait_for(lambda: reopened.snapshot()[0]["status"] == "Completed")
    finally:
        reopened.shutdown(1)


def test_second_queue_instance_rejected(queue_factory, tmp_path):
    queue_factory()
    with pytest.raises(TaskBusyError):
        QueueService(Settings(), tmp_path, resolver=FakeResolver(), engine_factory=lambda _: FakeEngine())


@pytest.mark.parametrize("bad", ["{broken", '{"schema_version":99,"tasks":[]}', '{"schema_version":1,"tasks":[{"status":"Completed"}]}'])
def test_corrupted_queue_never_overwritten(tmp_path, bad):
    path = tmp_path / "UMD_QUEUE.json"
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(StorageError):
        QueueService(Settings(), tmp_path)
    assert path.read_text(encoding="utf-8") == bad


def test_recover_interrupted_active_state_as_paused(tmp_path):
    engine = FakeEngine()
    settings = Settings(output_path=str(tmp_path / "output"))
    queue = QueueService(settings, tmp_path, resolver=FakeResolver(), engine_factory=lambda _: engine)
    queue.enqueue(analysis())
    wait_for(lambda: queue.snapshot()[0]["status"] == "Completed")
    queue.shutdown(1)
    state = json.loads(queue.path.read_text(encoding="utf-8"))
    state["tasks"][0]["status"] = "Processing"
    queue.path.write_text(json.dumps(state), encoding="utf-8")
    reopened = QueueService(settings, tmp_path, resolver=FakeResolver(), engine_factory=lambda _: FakeEngine())
    try:
        assert reopened.snapshot()[0]["status"] == "Paused"
    finally:
        reopened.shutdown(1)


def test_invalid_options_and_partial_playlist_do_not_modify_queue(queue_factory):
    queue, _ = queue_factory()
    with pytest.raises(ConfigurationError):
        queue.enqueue(analysis(), {"quality": "unknown"})
    with pytest.raises(ConfigurationError):
        queue.enqueue({"source": "youtube", "entries": [analysis(), {"id": "bad"}]})
    assert queue.snapshot() == []


def test_persistent_download_preferences_and_legacy_config_migration(tmp_path):
    store = SettingsStore(tmp_path)
    store.path.write_text(json.dumps({"locale": "ru-RU", "localization": False}), encoding="utf-8")
    settings = store.load()
    assert settings.download_type == "video"
    settings.ffmpeg_path = "C:/Tools/ffmpeg.exe"
    settings.download_type = "audio"
    settings.download_container = "mp3"
    settings.download_subtitles = "ru"
    settings.download_thumbnail = True
    store.save(settings)
    assert store.load() == settings
