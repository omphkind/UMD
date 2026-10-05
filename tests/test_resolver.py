from unittest.mock import Mock

import pytest

from app.core.config import Settings
from app.core.errors import MediaError
from app.sources.resolver import SourceResolver, validate_url


def test_generic_extractor_drives_source_and_actual_formats_not_site_list():
    backend = Mock()
    backend.extract.return_value = {
        "id": "example-1", "extractor": "PreviouslyUnknownProvider", "title": "Actual title", "webpage_url": "https://media.example/item/1",
        "formats": [{"format_id": "v1080", "ext": "webm", "height": 1080, "vcodec": "vp9", "acodec": "none"},
                    {"format_id": "audio", "ext": "opus", "vcodec": "none", "acodec": "opus"}],
        "subtitles": {"ru": [{"ext": "vtt"}]}, "automatic_captions": {"en": [{"ext": "vtt"}]},
        "chapters": [{"title": "Intro", "start_time": 0, "end_time": 10}], "thumbnail": "https://media.example/thumb.jpg",
    }
    analysis = SourceResolver(Settings(localization=False), backend).analyze("https://media.example/item/1")
    assert analysis["source"] == "previouslyunknownprovider"
    assert analysis["extractor"] == "PreviouslyUnknownProvider"
    assert analysis["qualities"] == [1080]
    assert len(analysis["formats"]) == 2
    assert analysis["subtitle_languages"] == ["en", "ru"]
    assert set(analysis["capabilities"]) == {"metadata", "video", "audio", "subtitles", "chapters", "thumbnail"}
    backend.extract.assert_called_once_with("https://media.example/item/1", flat=True)


def test_flat_playlist_dedup_and_shorts_metadata_no_fake_formats():
    backend = Mock()
    backend.extract.return_value = {"id": "list", "extractor": "YoutubeTab", "title": "Channel", "entries": [
        {"_type": "url", "id": "abcdefghijk", "url": "https://youtube.com/watch?v=abcdefghijk", "ext": "mp4", "title": "First"},
        {"webpage_url": "https://youtube.com/@test/shorts", "entries": [
            {"_type": "url", "id": "12345678901", "url": "https://youtube.com/shorts/12345678901", "title": "Short"},
            {"_type": "url", "id": "abcdefghijk", "url": "https://youtu.be/abcdefghijk", "title": "Repeated"},
            None,
        ]},
    ]}
    analysis = SourceResolver(Settings(localization=False), backend).analyze("https://youtube.com/@test")
    assert analysis["media_type"] == "playlist"
    assert len(analysis["entries"]) == 2
    assert analysis["entries"][0]["id"] == "abcdefghijk"
    assert analysis["entries"][0]["url"] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert analysis["entries"][0]["formats"] == []
    assert analysis["entries"][0]["needs_analysis"]
    assert analysis["entries"][1]["is_shorts"]


def test_youtube_adapter_localizes_original_values_retained():
    backend, localizer = Mock(), Mock()
    backend.extract.return_value = {"id": "abcdefghijk", "extractor_key": "Youtube", "title": "Original", "description": "Original description",
                                    "formats": [{"format_id": "18", "ext": "mp4", "height": 360, "vcodec": "avc1", "acodec": "mp4a"}]}
    localizer.fetch.return_value = {"title": "Русский заголовок", "description": "Описание", "description_found": True}
    analysis = SourceResolver(Settings(), backend, localizer).analyze("https://youtu.be/abcdefghijk")
    assert analysis["title"] == "Русский заголовок"
    assert analysis["original_title"] == "Original"
    assert analysis["description_source"] == "youtube_localized"
    assert analysis["original_description"] == "Original description"


def test_analysis_failure_is_preserved_not_fake_supported_source():
    backend = Mock()
    backend.extract.side_effect = MediaError("unsupported_url", "Unsupported URL")
    with pytest.raises(MediaError) as error:
        SourceResolver(Settings(), backend).analyze("https://unknown.example/notmedia")
    assert error.value.reason == "unsupported_url"


def test_drm_formats_do_not_become_fake_downloadable_fallback():
    backend = Mock()
    backend.extract.return_value = {"id": "locked", "extractor": "provider", "title": "Protected", "url": "https://media.example/protected.mp4", "ext": "mp4",
                                    "formats": [{"format_id": "drm", "ext": "mp4", "height": 1080, "vcodec": "avc1", "has_drm": True}]}
    analysis = SourceResolver(Settings(localization=False), backend).analyze("https://media.example/protected")
    assert analysis["formats"] == [] and analysis["qualities"] == []
    assert "video" not in analysis["capabilities"]


@pytest.mark.parametrize("url", ["file:///C:/local/video.mp4", "--help", "https://user:password@example.com/video", "https://example.com:bad/", "https://example.com/\n--exec"])
def test_invalid_url_never_reaches_backend(url):
    with pytest.raises(MediaError):
        validate_url(url)
