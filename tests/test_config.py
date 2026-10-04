import json

import pytest

from app.core.config import Settings, SettingsStore, default_data_dir
from app.core.errors import ConfigurationError, StorageError


def test_preferences_persist_separate_from_progress(tmp_path):
    store = SettingsStore(tmp_path)
    default = store.load()
    assert default.locale == "ru-RU"
    assert default.output_path == str(tmp_path / "output")
    assert not default.include_shorts and not default.include_members_only
    default.yt_dlp_path = "C:/Tools/yt-dlp.exe"
    default.debug = True
    store.save(default)
    assert SettingsStore(tmp_path).load() == default
    assert not (tmp_path / "UMD_PROGRESS.json").exists()


def test_default_data_dir_uses_windows_local_app_data(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_data_dir() == tmp_path / "UMD"


@pytest.mark.parametrize("data", [{"order": "shuffle"}, {"debug": "false"}, {"yt_dlp_path": 42}, {"locale": ""}, {"translation": True}])
def test_invalid_preferences_rejected(data):
    with pytest.raises(ConfigurationError):
        Settings.from_dict(data)


def test_corrupted_config_preserved(tmp_path):
    store = SettingsStore(tmp_path)
    store.path.write_text("{bad", encoding="utf-8")
    with pytest.raises(StorageError):
        store.load()
    with pytest.raises(StorageError):
        store.save(Settings())
    assert store.path.read_text(encoding="utf-8") == "{bad"


def test_invalid_existing_settings_preserved(tmp_path):
    store = SettingsStore(tmp_path)
    store.path.write_text(json.dumps({"order": "bad"}), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        store.save(Settings())
    assert json.loads(store.path.read_text(encoding="utf-8"))["order"] == "bad"
