"""Exercise the public console workflows with real tasks/metadata/storage."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Settings, SettingsStore
from app.core.errors import MediaError
from app.storage.progress import ProgressStore
from app.ui import application, cli


CHANNEL = "https://www.youtube.com/@fixture"


class FakeYouTubeSource:
    name = "youtube"

    def __init__(self, settings, dataset, calls):
        self.settings = settings
        self.dataset = dataset
        self.calls = calls
        self.downloader = SimpleNamespace(check=lambda: "2026.10.04")

    def discover(self, url):
        self.calls["discover"].append(url)
        return [{"media_id": media_id,
                 "url": f"https://www.youtube.com/watch?v={media_id}",
                 "published_at": str(raw.get("upload_date") or ""),
                 "is_shorts": raw.get("is_shorts", False)}
                for media_id, raw in self.dataset.items()]

    def fetch(self, url):
        media_id = url.partition("v=")[2]
        self.calls["fetch"].append(media_id)
        raw = self.dataset[media_id]
        if "error" in raw:
            raise MediaError(raw["error"], "Fixture unavailable")
        return {"id": media_id, **raw}


@pytest.fixture
def source_fixture(monkeypatch):
    dataset = {
        "aaaaaaaaaaa": {"title": "Первое видео", "description": "Обычное описание", "upload_date": "20240101"},
        "bbbbbbbbbbb": {"title": "Второе видео", "description": "Ещё описание", "upload_date": "20240201"},
    }
    calls = {"discover": [], "fetch": []}
    monkeypatch.setattr(application, "YouTubeSource", lambda settings: FakeYouTubeSource(settings, dataset, calls))
    return dataset, calls


def invoke(tmp_path, *arguments):
    return cli.main(["--data-dir", str(tmp_path), *arguments])


def configure(tmp_path, **values):
    args = ["settings"]
    for key, value in {"localization": False, **values}.items():
        args.extend(["--set", f"{key}={str(value).lower() if isinstance(value, bool) else value}"])
    assert invoke(tmp_path, *args) == 0


def test_cli_settings_persist_and_boolean_aliases(tmp_path, capsys):
    assert invoke(tmp_path, "settings", "--set", "debug=on", "--set", "include_shorts=1", "--set", "locale=en-US") == 0
    settings = SettingsStore(tmp_path).load()
    assert settings.debug and settings.include_shorts
    assert settings.locale == "en-US"
    capsys.readouterr()
    assert invoke(tmp_path, "settings") == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["debug"] is True
    assert shown["locale"] == "en-US"


def test_cli_process_resume_update_use_real_processor_and_durable_exports(tmp_path, source_fixture, capsys):
    dataset, calls = source_fixture
    configure(tmp_path)
    assert invoke(tmp_path, "process", CHANNEL) == 0
    store = ProgressStore(tmp_path / "UMD_PROGRESS.json")
    first = store.load()
    original = first.items["youtube:aaaaaaaaaaa"].to_dict()
    assert first.counts["success"] == 2
    assert calls["fetch"] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert (tmp_path / "output" / "UMD_URL.txt").read_text(encoding="utf-8").splitlines() == [
        "https://www.youtube.com/watch?v=aaaaaaaaaaa", "https://www.youtube.com/watch?v=bbbbbbbbbbb"]
    assert "title=Первое видео" in (tmp_path / "output" / "UMD_META.txt").read_text(encoding="utf-8")
    calls["fetch"].clear()
    calls["discover"].clear()
    assert invoke(tmp_path, "resume") == 0
    assert calls == {"fetch": [], "discover": []}
    dataset["ccccccccccc"] = {"title": "Новое видео", "description": "Новое описание", "upload_date": "20240301"}
    assert invoke(tmp_path, "update") == 0
    assert calls["fetch"] == ["ccccccccccc"]
    updated = store.load()
    assert updated.items["youtube:aaaaaaaaaaa"].to_dict() == original
    assert updated.items["youtube:ccccccccccc"].number == 3
    assert len((tmp_path / "output" / "UMD_URL.txt").read_text(encoding="utf-8").splitlines()) == 3
    assert "Результат:" in capsys.readouterr().out


def test_cli_retry_only_failed_items_preserves_id_and_number(tmp_path, source_fixture):
    dataset, calls = source_fixture
    dataset["bbbbbbbbbbb"]["error"] = "network"
    configure(tmp_path)
    assert invoke(tmp_path, "process", CHANNEL) == 0
    store = ProgressStore(tmp_path / "UMD_PROGRESS.json")
    failed = store.load().items["youtube:bbbbbbbbbbb"]
    assert failed.status == "error"
    number = failed.number
    calls["fetch"].clear()
    del dataset["bbbbbbbbbbb"]["error"]
    assert invoke(tmp_path, "retry") == 0
    assert calls["fetch"] == ["bbbbbbbbbbb"]
    retried = store.load()
    assert retried.items["youtube:bbbbbbbbbbb"].number == number
    assert retried.counts["success"] == 2
    assert len(retried.items) == 2


def test_cli_task_snapshot_retains_output_and_preferences_after_defaults_change(tmp_path, source_fixture):
    dataset, calls = source_fixture
    dataset["aaaaaaaaaaa"]["is_shorts"] = True
    original_output = tmp_path / "original-output"
    configure(tmp_path, output_path=original_output, include_shorts=False)
    assert invoke(tmp_path, "process", CHANNEL, "--include-shorts") == 0
    assert not SettingsStore(tmp_path).load().include_shorts
    assert ProgressStore(tmp_path / "UMD_PROGRESS.json").load().settings["include_shorts"] is True
    new_output = tmp_path / "new-output"
    configure(tmp_path, output_path=new_output)
    dataset["ccccccccccc"] = {"title": "New Short", "is_shorts": True}
    calls["fetch"].clear()
    assert invoke(tmp_path, "update") == 0
    assert calls["fetch"] == ["ccccccccccc"]
    assert len((original_output / "UMD_URL.txt").read_text(encoding="utf-8").splitlines()) == 3
    assert not (new_output / "UMD_URL.txt").exists()
    assert invoke(tmp_path, "export") == 0
    assert not (new_output / "UMD_URL.txt").exists()


@pytest.mark.parametrize("arguments", [("settings", "--set", "debug=maybe"), ("settings", "--set", "order=shuffle"), ("settings", "--set", "missing=true"), ("settings", "--set", "debug")])
def test_cli_invalid_settings_are_friendly_and_preserve_config(tmp_path, capsys, arguments):
    configure(tmp_path, debug=False)
    previous = (tmp_path / "config.json").read_bytes()
    assert invoke(tmp_path, *arguments) == 1
    error = capsys.readouterr().err
    assert "Ошибка:" in error
    assert "Traceback" not in error
    assert (tmp_path / "config.json").read_bytes() == previous


def test_cli_malformed_command_arguments_show_usage_without_traceback(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        invoke(tmp_path, "process", CHANNEL, "--order", "shuffle")
    assert exit_info.value.code == 2
    error = capsys.readouterr().err
    assert "invalid choice" in error
    assert "Traceback" not in error


def test_cli_missing_previous_task_is_friendly(tmp_path, source_fixture, capsys):
    configure(tmp_path)
    assert invoke(tmp_path, "resume") == 1
    assert "Предыдущая задача не найдена" in capsys.readouterr().err


def test_menu_startup_uses_saved_settings_without_asking_preferences(tmp_path, monkeypatch, capsys):
    configure(tmp_path, locale="en-US")
    checked = []
    prompts = []
    monkeypatch.setattr(cli, "environment_check", lambda settings, path: checked.append((settings.locale, path)) or True)

    def answer(prompt):
        prompts.append(prompt)
        return "0"

    monkeypatch.setattr("builtins.input", answer)
    assert invoke(tmp_path) == 0
    assert checked == [("en-US", tmp_path)]
    assert prompts == ["> "]
    output = capsys.readouterr().out
    assert "1. Новая обработка" in output
    assert "4. Повторить только ошибки" in output


def test_menu_settings_error_does_not_close_menu(tmp_path, monkeypatch, capsys):
    configure(tmp_path)
    monkeypatch.setattr(cli, "environment_check", lambda *_: True)
    answers = iter(["5", "debug=maybe", "0"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert invoke(tmp_path) == 0
    assert SettingsStore(tmp_path).load().debug is False
    assert "Ошибка:" in capsys.readouterr().out


def test_explicit_export_expands_home_directory_like_process(tmp_path, source_fixture, monkeypatch):
    fake_home = tmp_path / "user"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _: fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    configure(tmp_path, output_path="~/umd-export")
    assert invoke(tmp_path, "process", CHANNEL) == 0
    target = fake_home / "umd-export" / "UMD_URL.txt"
    assert target.exists()
    target.unlink()
    assert invoke(tmp_path, "export") == 0
    assert target.exists()
