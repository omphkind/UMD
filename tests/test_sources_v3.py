from unittest.mock import Mock

import pytest

from app.core.config import Settings
from app.core.errors import MediaError
from app.core.models import MediaItem
from app.sources.authentication import AuthenticationManager, public_metadata
from app.sources.gallery_dl_source import GalleryDlSource, normalize_gallery_item
from app.sources.resolver import SourceResolver


def test_audio_normalization_independent_of_provider():
    backend = Mock()
    backend.extract.return_value = {"id": "track", "extractor": "SoundCloud", "title": "Track", "artist": "Artist", "album": "Album",
                                    "track_number": 3, "release_year": 2024, "genre": "Electronic", "thumbnail": "https://example.org/cover.jpg",
                                    "formats": [{"format_id": "audio", "ext": "mp3", "vcodec": "none", "acodec": "mp3", "abr": 128}]}
    result = SourceResolver(Settings(localization=False), backend).analyze("https://soundcloud.com/artist/track")
    assert result["media_type"] == "audio" and result["downloadable"]
    assert result["metadata"]["artist"] == "Artist" and result["metadata"]["album"] == "Album"
    assert result["metadata"]["track_number"] == 3 and result["metadata"]["year"] == "2024"
    assert result["capability_status"] == "supported"


def test_youtube_music_is_audio_without_removing_available_video_formats():
    backend = Mock()
    backend.extract.return_value = {"id": "abcdefghijk", "extractor": "Youtube", "title": "Song", "artist": "Actual artist",
                                    "formats": [{"format_id": "18", "ext": "mp4", "vcodec": "h264", "acodec": "aac"}]}
    result = SourceResolver(Settings(localization=False), backend).analyze("https://music.youtube.com/watch?v=abcdefghijk")
    backend.extract.assert_called_once_with("https://www.youtube.com/watch?v=abcdefghijk", flat=False)
    assert result["media_type"] == "audio" and result["video_formats"]
    assert result["metadata"]["artist"] == "Actual artist"
    assert result["input_url"] == "https://music.youtube.com/watch?v=abcdefghijk"
    assert result["source_context"] == "youtube_music" and result["media_context"] == "audio"


def test_youtube_music_collection_uses_canonical_backend_url_and_audio_context_without_fake_formats():
    backend = Mock()
    backend.extract.return_value = {"id": "PL1234567890123", "extractor": "YoutubeTab", "title": "Music playlist", "entries": [
        {"_type": "url", "id": "abcdefghijk", "title": "Song", "url": "https://music.youtube.com/watch?v=abcdefghijk"}]}
    original = "https://music.youtube.com/playlist?list=PL1234567890123&feature=share"
    result = SourceResolver(Settings(localization=False), backend).analyze(original)
    backend.extract.assert_called_once_with("https://www.youtube.com/playlist?list=PL1234567890123", flat=True)
    assert result["input_url"] == original and result["source_context"] == "youtube_music"
    assert result["media_type"] == "playlist" and result["collection_media_types"] == ["audio"]
    entry = result["entries"][0]
    assert entry["media_type"] == "audio" and entry["needs_analysis"]
    assert entry["url"] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert not entry["formats"] and not entry["audio_formats"] and "audio" not in entry["capabilities"]


def test_flat_audio_extension_is_context_not_a_fabricated_download_format():
    backend = Mock()
    backend.extract.return_value = {"extractor": "SoundcloudSet", "entries": [{"_type": "url", "id": "track", "url": "https://soundcloud.com/artist/track", "ext": "mp3", "title": "Track"}]}
    result = SourceResolver(Settings(localization=False), backend).analyze("https://soundcloud.com/artist/sets/list")
    item = result["entries"][0]
    assert item["media_type"] == "audio" and item["needs_analysis"] and not item["audio_formats"]
    assert item["capabilities"] == ["metadata"]


def test_spotify_recognition_does_not_fabricate_metadata_or_downloads():
    backend = Mock()
    result = SourceResolver(Settings(), backend).analyze("https://open.spotify.com/track/realTrackIdentifier")
    backend.extract.assert_not_called()
    assert result["source"] == "spotify" and result["capability_status"] == "metadata_only"
    assert not result["downloadable"] and result["formats"] == []
    assert "официальный API" in result["capability_reason"]


@pytest.mark.parametrize("source,url", [
    ("instagram", "https://www.instagram.com/p/post/"), ("vk", "https://vk.com/album1_2"),
    ("pinterest", "https://www.pinterest.com/owner/board/"), ("flickr", "https://www.flickr.com/photos/user/albums/album"),
])
def test_gallery_provider_support_comes_from_backend(source, url):
    gallery, video = Mock(), Mock()
    gallery.supports.return_value = True
    gallery.analyze.return_value = {"source": source, "media_type": "playlist", "entries": [], "has_more": True}
    result = SourceResolver(Settings(), video, gallery_backend=gallery).analyze(url, page=3, page_size=40)
    assert result["source"] == source
    gallery.supports.assert_called_once_with(url, None)
    gallery.analyze.assert_called_once_with(url, control=None, page=3, page_size=40)
    video.extract.assert_not_called()


def test_gallery_authentication_failure_returns_status_and_reason():
    gallery = Mock()
    gallery.supports.return_value = True
    gallery.analyze.side_effect = MediaError("authentication_required", "Authentication required")
    result = SourceResolver(Settings(), Mock(), gallery_backend=gallery).analyze("https://example.org/profile")
    assert result["capability_status"] == "authentication_required"
    assert result["capability_reason"] == "Authentication required" and not result["downloadable"]


def test_gallery_item_identity_is_stable_across_signed_url_refresh_and_post_images_distinct():
    raw = {"id": 42, "category": "photos", "filename": "Image", "extension": "jpg", "download_url": "https://cdn.example/image.jpg?token=old", "num": 1}
    first = normalize_gallery_item(raw, "https://example/post/42")
    second = normalize_gallery_item({**raw, "download_url": "https://cdn.example/image.jpg?token=new"}, "https://example/post/42")
    other = normalize_gallery_item({**raw, "num": 2}, "https://example/post/42")
    assert first["id"] == second["id"] != other["id"]
    assert first["photo_formats"][0]["format_id"] == "original"
    assert first["metadata"]["source_id"] == first["id"]


def test_gallery_pagination_stops_owned_process_after_bounded_window(monkeypatch, tmp_path):
    settings = Settings(gallery_dl_path=str(tmp_path / "gallery-dl.exe"))
    (tmp_path / "gallery-dl.exe").touch()
    consumed = []
    import json
    def stream(args, on_line, **kwargs):
        assert "--no-download" in args and "--dump-json" not in args
        assert args[args.index("--cache-file") + 1] == ":memory:"
        assert "extractor.postprocessors=" in " ".join(args)
        for index in range(100000):
            consumed.append(index)
            on_line(json.dumps({"category": "photos", "subcategory": "profile", "filename": str(index), "extension": "jpg", "download_url": f"https://example.org/{index}.jpg"}))
        return 0, ""
    monkeypatch.setattr("app.sources.gallery_dl_source.run_process", stream)
    result = GalleryDlSource(settings).analyze("https://example.org/profile", page=2, page_size=25)
    assert len(consumed) == 51 and len(result["entries"]) == 25
    assert result["entries"][0]["gallery_index"] == 26
    assert result["has_more"] and result["next_page"] == 3 and result["total_count"] is None


def test_partial_gallery_error_preserves_enumerated_items(monkeypatch, tmp_path):
    tool = tmp_path / "gallery-dl.exe"
    tool.touch()
    import json
    def stream(args, on_line, **kwargs):
        for index in range(2):
            on_line(json.dumps({"category": "photos", "subcategory": "album", "filename": str(index), "extension": "jpg", "download_url": f"https://example.org/{index}.jpg"}))
        return 1, "Network connection interrupted"
    monkeypatch.setattr("app.sources.gallery_dl_source.run_process", stream)
    result = GalleryDlSource(Settings(gallery_dl_path=str(tool))).analyze("https://example.org/album")
    assert len(result["entries"]) == 2
    assert result["capability_status"] == "supported_with_limitations"
    assert result["total_count"] is None and "interrupted" in result["capability_reason"]


def test_explicit_cookie_file_only_and_secret_redaction(tmp_path):
    cookie = tmp_path / "private-cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    settings = Settings(auth_mode="cookies", cookie_file=str(cookie))
    auth = AuthenticationManager(settings)
    assert auth.arguments() == ["--cookies", str(cookie.resolve())]
    assert AuthenticationManager(Settings(cookie_file=str(cookie))).arguments() == []
    text = auth.redact(f"Cannot read {cookie}\nCookie: sessionid=secret\nAuthorization: Bearer other\nURL ?token=third&x=1")
    assert all(secret not in text for secret in (str(cookie), "secret", "other", "third"))
    assert public_metadata({"title": "Safe", "http_headers": {"Cookie": "secret"}, "password": "bad", "nested": {"access_token": "bad"}}) == {"title": "Safe", "nested": {}}


def test_universal_media_model_old_serialization_and_new_fields():
    old = {"source": "youtube", "media_id": "abcdefghijk", "url": "https://youtu.be/abcdefghijk", "number": 1}
    item = MediaItem.from_dict(old)
    assert item.media_type == "video" and item.source_id == item.media_id
    photo = MediaItem.from_dict({**old, "source": "photos", "media_type": "photo", "collection_type": "gallery", "collection_id": "album", "collection_index": 7, "dimensions": {"width": 640, "height": 480}})
    assert MediaItem.from_dict(photo.to_dict()).collection_index == 7
