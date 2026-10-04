from unittest.mock import Mock

import pytest

from app.core.config import Settings
from app.core.errors import MediaError
from app.sources.youtube import YouTubeSource, is_members_only, parse_youtube_url, shorts_signals


ID = "aBcDeFgH_12"


@pytest.mark.parametrize("url,kind,identifier", [
    (f"https://youtube.com/watch?v={ID}&list=PL1234567890&t=15", "video", ID),
    (f"https://youtu.be/{ID}?si=abc", "video", ID),
    (f"https://m.youtube.com/shorts/{ID}", "video", ID),
    (f"https://www.youtube.com/embed/{ID}", "video", ID),
    ("https://youtube.com/@test/videos", "channel", "@test"),
    ("https://www.youtube.com/channel/UC1234567890123456789012", "channel", "channel/UC1234567890123456789012"),
    ("https://youtube.com/playlist?list=PL1234567890123", "playlist", "PL1234567890123"),
])
def test_parse_resource(url, kind, identifier):
    parsed = parse_youtube_url(url)
    assert (parsed.kind, parsed.identifier) == (kind, identifier)


@pytest.mark.parametrize("url", [
    "https://youtube.com.evil.test/watch?v=" + ID,
    "https://youtube.com@evil.test/watch?v=" + ID,
    "file:///youtube.com/watch?v=" + ID,
    "https://youtube.com/watch?v=short",
    "https://youtu.be/" + ID + "/extra",
    "https://youtube.com:bad/watch?v=" + ID,
    "https://youtube.com/channel/notchannelid", "--help", "", None,
])
def test_invalid_url(url):
    with pytest.raises(MediaError) as error:
        parse_youtube_url(url)
    assert error.value.reason == "unsupported_url"


def test_same_id_canonical_and_individual_discovery_does_not_extract():
    downloader = Mock()
    source = YouTubeSource(Settings(), downloader)
    first = source.discover(f"https://youtube.com/watch?v={ID}")[0]
    second = source.discover(f"https://youtu.be/{ID}")[0]
    assert first["media_id"] == second["media_id"] == ID
    assert first["url"] == second["url"]
    downloader.extract.assert_not_called()


def test_flat_channel_includes_nested_shorts_deduplicates_and_preserves_unavailable():
    downloader = Mock()
    downloader.extract.return_value = {"entries": [
        {"entries": [{"id": ID, "title": "Ordinary"}]},
        {"webpage_url": "https://youtube.com/@test/shorts", "entries": [
            {"id": ID, "title": "Same video"},
            {"id": "abcdefghijk", "title": "[Private video]", "availability": "private"},
            None,
        ]},
    ]}
    items = YouTubeSource(Settings(), downloader).discover("https://youtube.com/@test")
    assert len(items) == 2
    assert items[0]["is_shorts"] is True
    assert items[1]["media_id"] == "abcdefghijk"
    assert items[1]["metadata"]["availability"] == "private"
    downloader.extract.assert_called_once_with("https://www.youtube.com/@test", flat=True)


@pytest.mark.parametrize("data", [
    {"is_short": True}, {"is_shorts": True}, {"media_type": "shorts"},
    {"source_tab": "shorts"}, {"webpage_url": f"https://youtube.com/shorts/{ID}"},
])
def test_shorts_evidence(data):
    assert shorts_signals(data)


def test_duration_and_portrait_thumbnail_do_not_mark_regular_video_as_shorts():
    assert shorts_signals({"duration": 30, "thumbnails": [{"width": 100, "height": 200}]}) == []


@pytest.mark.parametrize("data", [
    {"availability": "subscriber_only"}, {"is_members_only": True},
    {"message": "This video is available to this channel's members"},
])
def test_members_only_evidence(data):
    assert is_members_only(data)


def test_needs_auth_is_not_members_only():
    assert not is_members_only({"availability": "needs_auth"})
