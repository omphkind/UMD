import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from app.core.config import Settings
from app.core.errors import MediaError, SkipItem, StorageError, TaskBusyError
from app.core.models import MediaItem, Progress
from app.storage import atomic
from app.storage.lock import FileLock
from app.storage.progress import ProgressStore
from app.tasks.service import TaskService


def entry(media_id, **extra):
    return {"media_id": media_id, "url": f"https://youtube.com/watch?v={media_id}", "title": media_id, **extra}


class FakeSource:
    name = "youtube"

    def __init__(self, entries):
        self.entries = entries

    def discover(self, url):
        return list(self.entries)


class FakeProcessor:
    def __init__(self, failures=None):
        self.calls = []
        self.failures = failures or {}

    def process(self, item):
        self.calls.append(item.media_id)
        if item.media_id in self.failures:
            raise self.failures[item.media_id]
        return {"title": f"Title {item.media_id}", "original_title": item.media_id, "overview": "Description"}


def service(tmp_path, entries, processor=None, settings=None):
    return TaskService(ProgressStore(tmp_path / "UMD_PROGRESS.json"), FakeSource(entries), processor or FakeProcessor(), settings)


def test_save_load_and_backup(tmp_path):
    store = ProgressStore(tmp_path / "UMD_PROGRESS.json")
    progress = Progress("youtube", ["channel"], Settings().to_dict())
    progress.items["youtube:aaa"] = MediaItem("youtube", "aaa", "https://youtube.com/watch?v=aaa", 1)
    store.save(progress)
    progress.items["youtube:aaa"].status = "success"
    store.save(progress)
    assert store.load().items["youtube:aaa"].status == "success"
    backup = json.loads(Path(str(store.path) + ".bak").read_text(encoding="utf-8"))
    assert backup["items"]["youtube:aaa"]["status"] == "pending"


def test_failure_does_not_stop_queue_and_retry_keeps_number(tmp_path):
    processor = FakeProcessor({"bbb": MediaError("network", "Network failed")})
    task = service(tmp_path, [entry("aaa"), entry("bbb"), entry("ccc")], processor)
    progress = task.new("channel")
    assert set(processor.calls) == {"aaa", "bbb", "ccc"}
    assert progress.counts["success"] == 2
    assert progress.counts["error"] == 1
    assert progress.history["last_error"]["reason"] == "network"
    original_number = progress.items["youtube:bbb"].number
    processor.failures.clear()
    processor.calls.clear()
    result = task.retry_errors()
    assert processor.calls == ["bbb"]
    assert result.items["youtube:bbb"].number == original_number
    assert result.counts["success"] == 3
    assert result.history["last_successful_run"]


def test_resume_recovers_processing_and_ignores_finished_states(tmp_path):
    task = service(tmp_path, [entry("aaa"), entry("bbb"), entry("ccc"), entry("ddd")])
    progress = task.new("channel")
    progress.items["youtube:aaa"].status = "processing"
    progress.items["youtube:bbb"].status = "pending"
    progress.items["youtube:ccc"].status = "error"
    progress.items["youtube:ddd"].status = "skipped"
    task.store.save(progress)
    task.processor.calls.clear()
    result = task.resume()
    assert set(task.processor.calls) == {"aaa", "bbb"}
    assert result.items["youtube:ccc"].status == "error"
    assert result.items["youtube:ddd"].status == "skipped"
    assert any(event["event"] == "recovered" for event in result.history["events"])


def test_update_deduplicates_same_id_with_different_url_and_preserves_success(tmp_path):
    task = service(tmp_path, [entry("aaa"), entry("bbb")])
    first = task.new(["channel", "channel"])
    previous = first.items["youtube:aaa"].to_dict()
    last_number = max(item.number for item in first.items.values())
    task.source.entries = [entry("aaa", url="https://youtu.be/aaa"), entry("ccc"), entry("ccc"), entry("bbb")]
    task.processor.calls.clear()
    result = task.update()
    assert task.processor.calls == ["ccc"]
    assert result.items["youtube:aaa"].to_dict() == previous
    assert result.items["youtube:ccc"].number == last_number + 1
    assert result.history["found_count"] == 3
    assert result.history["new_count"] == 1
    assert result.history["last_updated"]
    task.processor.calls.clear()
    task.update()
    assert task.processor.calls == []
    assert len(task.store.load().items) == 3


def test_failed_initial_scan_preserves_task_settings_and_update_recovers(tmp_path):
    task = service(tmp_path, [], settings=Settings(include_shorts=True))
    task.source.discover = lambda _: (_ for _ in ()).throw(MediaError("network", "Offline"))
    with pytest.raises(MediaError):
        task.new("channel")
    persisted = task.store.load()
    assert persisted.source_urls == ["channel"]
    assert persisted.settings["include_shorts"] is True
    assert persisted.history["last_error"]["reason"] == "network"
    task.source.discover = lambda _: [entry("aaa")]
    assert task.update().counts["success"] == 1


def test_failed_update_preserves_successes_and_new_task_archives_old(tmp_path):
    task = service(tmp_path, [entry("aaa")])
    original = task.new("channel").items["youtube:aaa"].to_dict()
    task.source.discover = lambda _: (_ for _ in ()).throw(MediaError("network", "Offline"))
    with pytest.raises(MediaError):
        task.update()
    assert task.store.load().items["youtube:aaa"].to_dict() == original
    task.source.discover = lambda _: [entry("bbb")]
    new = task.new("other-channel")
    assert list(new.items) == ["youtube:bbb"]
    archives = list((tmp_path / "archive").glob("*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8"))["items"]["youtube:aaa"] == original


def test_skip_defaults_and_expected_processor_skips_are_not_errors(tmp_path):
    task = service(tmp_path, [entry("short", is_shorts=True), entry("member", is_members_only=True), entry("late")], FakeProcessor({"late": SkipItem("members_only")}))
    result = task.new("channel")
    assert task.processor.calls == ["late"]
    assert result.counts["skipped"] == 3
    assert result.counts["error"] == 0
    assert result.history["skip_reasons"] == {"members_only": 2, "shorts": 1}


def test_skip_detected_after_processing(tmp_path):
    class Processor:
        def process(self, item):
            return {"is_shorts": True, "title": "Short"}
    result = service(tmp_path, [entry("aaa")], Processor()).new("channel")
    assert result.items["youtube:aaa"].status == "skipped"
    assert result.items["youtube:aaa"].skip_reason == "shorts"


@pytest.mark.parametrize("invalid", [{"metadata": None}, {"metadata": {"bad": float("nan")}}, {"title": None}, {"is_shorts": "false"}])
def test_invalid_processor_result_does_not_stop_queue_or_corrupt_storage(tmp_path, invalid):
    class Processor:
        def process(self, item):
            return invalid if item.media_id == "bad" else {"title": "Good"}

    task = service(tmp_path, [entry("bad"), entry("good")], Processor())
    result = task.new("channel")
    assert result.items["youtube:bad"].status == "error"
    assert result.items["youtube:bad"].error_reason == "invalid_metadata"
    assert task.store.load().items["youtube:good"].status == "success"


def test_included_restricted_items_processed_when_enabled(tmp_path):
    settings = Settings(include_shorts=True, include_members_only=True)
    task = service(tmp_path, [entry("short", is_shorts=True), entry("member", is_members_only=True)], settings=settings)
    result = task.new("channel")
    assert result.counts["success"] == 2


def test_saved_preferences_not_new_defaults_control_resume(tmp_path):
    task = service(tmp_path, [entry("aaa", is_shorts=True)], settings=Settings(include_shorts=True))
    result = task.new("channel")
    result.items["youtube:aaa"].status = "pending"
    task.store.save(result)
    task.settings = Settings(include_shorts=False)
    assert task.resume().items["youtube:aaa"].status == "success"


def test_order_and_numbers_follow_publication_date(tmp_path):
    task = service(tmp_path, [entry("new", published_at="2025-01-01"), entry("old", published_at="2020-01-01")])
    result = task.new("channel")
    assert result.items["youtube:old"].number == 1
    assert task.processor.calls == ["old", "new"]


def test_discovery_accepts_unknown_publication_date(tmp_path):
    task = service(tmp_path, [entry("aaa", published_at=None)])
    assert task.new("channel").items["youtube:aaa"].published_at == ""


def test_each_item_persisted_before_and_after_processing(tmp_path):
    store = ProgressStore(tmp_path / "UMD_PROGRESS.json")
    calls = []

    class Processor:
        def process(self, item):
            stored = store.load()
            assert stored.items[item.key].status == "processing"
            if calls:
                assert stored.items[f"youtube:{calls[-1]}"].status == "success"
            calls.append(item.media_id)
            return {"title": item.media_id}

    TaskService(store, FakeSource([entry("aaa"), entry("bbb")]), Processor()).new("channel")
    assert store.load().counts["success"] == 2


def test_interruption_persists_pending_and_resume(tmp_path):
    task = service(tmp_path, [entry("aaa")], FakeProcessor({"aaa": KeyboardInterrupt()}))
    with pytest.raises(KeyboardInterrupt):
        task.new("channel")
    assert task.store.load().items["youtube:aaa"].status == "pending"
    task.processor.failures.clear()
    assert task.resume().counts["success"] == 1


@pytest.mark.parametrize("bad", ["{broken", '{"schema_version":99}', '{"schema_version":1,"source":"youtube","source_urls":[],"settings":{},"history":{},"items":{"youtube:aaa":{"source":"youtube","media_id":"bbb","url":"x","number":1}}}'])
def test_corruption_never_overwritten(tmp_path, bad):
    store = ProgressStore(tmp_path / "UMD_PROGRESS.json")
    store.path.write_text(bad, encoding="utf-8")
    with pytest.raises(StorageError):
        store.load()
    with pytest.raises(StorageError):
        store.save(Progress("youtube", ["channel"], Settings().to_dict()))
    assert store.path.read_text(encoding="utf-8") == bad


def test_atomic_replace_failure_keeps_previous_data_and_removes_temp(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    atomic.write_json(target, {"valid": True})
    original = target.read_bytes()
    monkeypatch.setattr(atomic.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("disk error")))
    with pytest.raises(StorageError):
        atomic.write_json(target, {"valid": False})
    assert target.read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == []


def test_thread_lock_prevents_second_task(tmp_path):
    errors = []
    lock_path = tmp_path / "task.lock"

    def competing():
        try:
            with FileLock(lock_path):
                pass
        except TaskBusyError as exc:
            errors.append(exc)

    with FileLock(lock_path):
        thread = threading.Thread(target=competing)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert len(errors) == 1
    with FileLock(lock_path):
        pass


def test_process_lock_is_released_when_process_dies(tmp_path):
    lock_path = tmp_path / "task.lock"
    script = "import sys,time; from app.storage.lock import FileLock\nwith FileLock(sys.argv[1]):\n print('ready',flush=True)\n time.sleep(30)\n"
    process = subprocess.Popen([sys.executable, "-c", script, str(lock_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(TaskBusyError):
            with FileLock(lock_path):
                pass
    finally:
        process.terminate()
        process.wait(timeout=5)
    with FileLock(lock_path):
        pass
