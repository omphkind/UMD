from unittest.mock import Mock

import pytest

from app.core.config import Settings
from app.core.errors import MediaError, SkipItem
from app.core.models import MediaItem
from app.metadata.cleanup import clean_description, clean_title
from app.metadata.youtube import YouTubeProcessor


def item(**kwargs):
    return MediaItem("youtube", "abcdefghijk", "https://youtube.com/watch?v=abcdefghijk", 1, **kwargs)


@pytest.mark.parametrize("title", ["Learn Python in 30 minutes", "Тренировка 30 минут", "1 hour, 12 minutes", None])
def test_title_does_not_remove_genuine_duration(title):
    assert clean_title(title) == (title or "")


@pytest.mark.parametrize("value,expected", [
    ("Название 30 minutes", "Название"),
    ("Название 1 hour, 12 minutes", "Название"),
    ("Видео 1 час, 12 минут", "Видео"),
    ("Видео 45 секунд", "Видео"),
])
def test_aria_duration(value, expected):
    assert clean_title(value, from_aria_label=True) == expected


def test_authoritative_original_prevents_accidental_duration_removal():
    original = "Learn Python in 30 minutes"
    assert clean_title(original, from_aria_label=True, original_title=original) == original
    assert clean_title(original + " 1 hour", from_aria_label=True, original_title=original) == original


def test_conservative_description_keeps_narrative_citations_and_lines():
    raw = "An essay about Instagram and the telegram in history.\n\n\nSource: https://example.org/article\nContact: test@example.com\nInstagram: https://instagram.com/user\nSPONSOR: Brand\n#tag #trailing"
    assert clean_description(raw) == "An essay about Instagram and the telegram in history.\n\nSource: https://example.org/article"


def test_social_url_host_boundary_preserves_similar_ordinary_domain():
    assert clean_description("Read https://notinstagram.com/article") == "Read https://notinstagram.com/article"


def test_hashtags_mid_description_preserved_and_empty_is_supported():
    assert clean_description("#science\nAn informative essay") == "#science\nAn informative essay"
    assert clean_description(None) == ""
    assert clean_description("\r\n\n") == ""


def test_localized_metadata_precedence_with_original_provenance():
    source = Mock()
    source.fetch.return_value = {"title": "Original", "description": "Original description", "upload_date": "20261003", "channel": "Creator", "duration": 900}
    localizer = Mock()
    localizer.fetch.return_value = {"title": "Русское название", "description": "Первый абзац\n\nВторой абзац"}
    result = YouTubeProcessor(source, Settings(), localizer).process(item())
    assert result["title"] == "Русское название"
    assert result["original_title"] == "Original"
    assert result["overview"] == "Первый абзац\n\nВторой абзац"
    assert result["original_description"] == "Original description"
    assert result["title_source"] == result["description_source"] == "youtube_localized"
    assert result["published_at"] == "2026-10-03"
    assert result["metadata"]["localized_values"]["ru-RU"]["title"] == "Русское название"


def test_localizer_failure_falls_back_to_original(caplog):
    source, localizer = Mock(), Mock()
    source.fetch.return_value = {"title": "Original", "description": "Description"}
    localizer.fetch.side_effect = MediaError("localization", "Browser unavailable")
    result = YouTubeProcessor(source, Settings(), localizer).process(item())
    assert result["title"] == "Original"
    assert result["description_source"] == "original"
    assert "Browser unavailable" in caplog.text


@pytest.mark.parametrize("flag,reason", [("is_shorts", "shorts"), ("is_members_only", "members_only")])
def test_excluded_items_skip_without_fetch(flag, reason):
    source = Mock()
    with pytest.raises(SkipItem) as error:
        YouTubeProcessor(source, Settings()).process(item(**{flag: True}))
    assert error.value.reason == reason
    source.fetch.assert_not_called()


def test_members_error_becomes_skip_and_flag_persists():
    source = Mock()
    source.fetch.side_effect = MediaError("members_only", "Join this channel")
    media = item()
    with pytest.raises(SkipItem) as error:
        YouTubeProcessor(source, Settings()).process(media)
    assert error.value.reason == "members_only"
    assert media.is_members_only


def test_enabled_members_access_failure_remains_error_no_bypass():
    source = Mock()
    source.fetch.side_effect = MediaError("members_only", "Join this channel")
    with pytest.raises(MediaError):
        YouTubeProcessor(source, Settings(include_members_only=True)).process(item())
    source.fetch.assert_called_once()


def test_missing_title_is_explicit_failure():
    source = Mock()
    source.fetch.return_value = {"description": "Has no title"}
    with pytest.raises(MediaError) as error:
        YouTubeProcessor(source, Settings(localization=False)).process(item())
    assert error.value.reason == "missing_metadata"


def test_legitimate_empty_localized_description_differs_from_missing_dom():
    source, localizer = Mock(), Mock()
    source.fetch.return_value = {"title": "Original", "description": "Original description"}
    localizer.fetch.return_value = {"title": "Localized", "description": "", "description_found": True}
    result = YouTubeProcessor(source, Settings(), localizer).process(item())
    assert result["overview"] == ""
    assert result["description_source"] == "youtube_localized"
    localizer.fetch.return_value["description_found"] = False
    result = YouTubeProcessor(source, Settings(), localizer).process(item())
    assert result["overview"] == "Original description"
    assert result["description_source"] == "original"
