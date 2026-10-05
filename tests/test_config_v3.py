import json

import pytest

from app.core.config import Settings, SettingsStore
from app.core.errors import ConfigurationError


def test_legacy_configuration_acquires_media_gallery_and_rename_defaults(tmp_path):
    store = SettingsStore(tmp_path)
    store.path.write_text(json.dumps({"output_path": str(tmp_path / "media"), "download_quality": "720"}), encoding="utf-8")
    settings = store.load()
    assert settings.download_quality == "720"
    assert settings.default_video_quality == "720"
    assert settings.default_photo_quality == "original"
    assert settings.auth_mode == "anonymous" and settings.collision_policy == "ask"
    assert not settings.rename_before_download
    assert settings.simultaneous_downloads == 1


def test_new_preferences_and_cookie_reference_roundtrip_without_cookie_contents(tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("secret cookie fixture", encoding="utf-8")
    settings = Settings(gallery_dl_path="gallery-dl.exe", cookie_file=str(cookies), auth_mode="cookies",
                        simultaneous_downloads=4, default_audio_format="flac", default_photo_quality="large",
                        audio_bitrate="320", rename_before_download=True, rename_template="{artist} - {title}",
                        folder_organization=True, gallery_page_size=50, thumbnail_cache=False)
    store = SettingsStore(tmp_path)
    store.save(settings)
    assert store.load() == settings
    assert "secret cookie fixture" not in store.path.read_text(encoding="utf-8")


@pytest.mark.parametrize("values", [{"simultaneous_downloads": 0}, {"simultaneous_downloads": 9},
                                      {"simultaneous_downloads": True}, {"gallery_max_concurrency": 9},
                                      {"gallery_page_size": 501}, {"rename_number_step": 0},
                                      {"collision_policy": "silently_replace"}, {"auth_mode": "password"},
                                      {"audio_bitrate": "fast"}, {"embed_cover": "yes"},
                                      {"default_photo_format": "mp4"}, {"password": "secret"}])
def test_invalid_resource_settings_and_authentication_secrets_are_rejected(values):
    with pytest.raises(ConfigurationError):
        Settings.from_dict(values)


def test_photo_original_quality_is_valid_and_unicode_template_is_preserved():
    settings = Settings.from_dict({"download_type": "photo", "download_container": "original",
                                   "download_quality": "original", "rename_template": "旅行 — {title}"})
    assert settings.rename_template == "旅行 — {title}"


@pytest.mark.parametrize("kind,container,quality", [("video", "mkv", "720"),
                                                    ("video", "webm", "1080"),
                                                    ("audio", "mp3", "best"),
                                                    ("audio", "m4a", "format:audio-low")])
def test_legacy_media_preferences_migrate_to_matching_type_defaults(kind, container, quality):
    original = {"download_type": kind, "download_container": container, "download_quality": quality}
    settings = Settings.from_dict(original)
    assert getattr(settings, f"default_{kind}_format") == container
    assert getattr(settings, f"default_{kind}_quality") == quality
    assert original == {"download_type": kind, "download_container": container, "download_quality": quality}
    assert Settings.from_dict(settings.to_dict()) == settings


def test_explicit_new_preferences_are_not_overridden_by_legacy_active_selection():
    settings = Settings.from_dict({"download_type": "video", "download_container": "mkv", "download_quality": "720",
                                   "default_video_format": "mp4", "default_video_quality": "1080"})
    assert settings.default_video_format == "mp4" and settings.default_video_quality == "1080"


def test_legacy_audio_with_historical_video_container_keeps_valid_audio_default():
    settings = Settings.from_dict({"download_type": "audio", "download_container": "mp4"})
    assert settings.default_audio_format == "mp3"


@pytest.mark.parametrize("size,valid", [(1, True), (200, True), (201, False), (500, False)])
def test_gallery_page_size_matches_actual_source_pagination_limits(size, valid):
    if valid:
        assert Settings.from_dict({"gallery_page_size": size}).gallery_page_size == size
    else:
        with pytest.raises(ConfigurationError, match="gallery_page_size"):
            Settings.from_dict({"gallery_page_size": size})
