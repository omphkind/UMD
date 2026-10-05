import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.config import Settings
from app.core.errors import ConfigurationError, MediaError
from app.downloader.engine import DownloadOptions, YtDlpDownloader


@pytest.fixture
def engine(tmp_path):
    settings = Settings(output_path=str(tmp_path / "downloads"), localization=False)
    for name in ("yt-dlp", "deno", "ffmpeg"):
        executable = tmp_path / (name + ".exe")
        executable.touch()
        setattr(settings, {"yt-dlp": "yt_dlp_path", "deno": "deno_path", "ffmpeg": "ffmpeg_path"}[name], str(executable))
    return YtDlpDownloader(settings)


def track():
    return {"id": "track", "source": "music", "url": "https://example.org/track.mp3", "title": "Track", "media_type": "audio",
            "formats": [{"format_id": "native", "ext": "mp3", "vcodec": "none", "acodec": "mp3", "abr": 128}],
            "thumbnail": "https://example.org/cover.jpg"}


def test_audio_cover_metadata_and_bitrate_are_real_backend_options(engine):
    options = DownloadOptions.from_dict({"media_type": "audio", "container": "mp3", "audio_bitrate": "192k"}, engine.settings)
    args, _, _, _ = engine.build_command(track(), options)
    assert args[args.index("--audio-quality") + 1] == "192K"
    assert "--embed-metadata" in args and "--embed-thumbnail" in args
    assert args[args.index("--convert-thumbnails") + 1] == "jpg"
    assert args[args.index("--ffmpeg-location") + 1] == engine.settings.ffmpeg_path


@pytest.mark.parametrize("container", ["wav", "aac"])
def test_cover_for_unsupported_embedding_container_is_downloaded_beside_audio(engine, container):
    args, _, _, _ = engine.build_command(track(), DownloadOptions.from_dict({"media_type": "audio", "container": container}, engine.settings))
    assert "--write-thumbnail" in args and "--embed-thumbnail" not in args


def test_planned_unicode_filename_is_used_before_transfer(engine):
    target = Path(engine.settings.output_path) / "Музыка" / "歌 - 003 - Песня.mp3"
    options = DownloadOptions.from_dict({"media_type": "audio", "container": "mp3", "target_path": str(target)}, engine.settings)
    args, output, prefix, _ = engine.build_command(track(), options)
    assert output == target.parent.resolve() and prefix == target.stem
    assert args[args.index("--output") + 1] == str(target.with_suffix("")).replace("%", "%%") + ".%(ext)s"


def test_original_extension_must_match_real_format_before_transfer(engine):
    target = Path(engine.settings.output_path) / "wrong.flac"
    with pytest.raises(ConfigurationError):
        engine.build_command(track(), DownloadOptions.from_dict({"media_type": "audio", "container": "original", "target_path": str(target)}, engine.settings))


def test_original_audio_extension_comes_from_codec_not_video_container(engine):
    media = {**track(), "formats": [{"format_id": "combined", "ext": "mp4", "vcodec": "h264", "acodec": "mp4a.40.2"}]}
    wrong = Path(engine.settings.output_path) / "audio.mp4"
    with pytest.raises(ConfigurationError):
        engine.build_command(media, DownloadOptions.from_dict({"media_type": "audio", "container": "original", "target_path": str(wrong)}, engine.settings))
    target = wrong.with_suffix(".m4a")
    args, _, _, _ = engine.build_command(media, DownloadOptions.from_dict({"media_type": "audio", "container": "original", "target_path": str(target)}, engine.settings))
    assert args[args.index("--audio-format") + 1] == "best"


def test_metadata_false_writes_no_sidecar(engine, tmp_path):
    target = Path(engine.settings.output_path) / "Track.mp3"
    def transfer(args, on_line, **kwargs):
        target.write_bytes(b"real-media-for-command-test")
        on_line("UMD_FILE:" + json.dumps(str(target)))
        return 0, ""
    with patch("app.downloader.engine.run_process", side_effect=transfer):
        result = engine.download(track(), {"media_type": "audio", "container": "mp3", "target_path": str(target), "metadata_preserve": False, "embed_cover": False, "chapters": False})
    assert result["files"] == [str(target)]
    assert not list(target.parent.glob("*.json"))


def test_existing_sidecar_is_protected_before_transfer(engine):
    target = Path(engine.settings.output_path) / "Track.mp3"
    target.parent.mkdir()
    sidecar = target.with_name("Track.umd.json")
    sidecar.write_text("user file", encoding="utf-8")
    with patch("app.downloader.engine.run_process") as transfer:
        with pytest.raises(MediaError) as error:
            engine.download(track(), {"media_type": "audio", "container": "mp3", "target_path": str(target)})
    assert error.value.reason == "collision"
    transfer.assert_not_called()
    assert sidecar.read_text() == "user file"


def test_metadata_only_preserves_explicit_planned_json_path(engine):
    target = Path(engine.settings.output_path) / "planned.json"
    with patch("app.downloader.engine.run_process") as transfer:
        result = engine.download(track(), {"media_type": "metadata", "target_path": str(target), "chapters": False})
    assert result["files"] == [str(target)]
    assert json.loads(target.read_text(encoding="utf-8"))["analysis"]["id"] == "track"
    transfer.assert_not_called()


def test_planned_thumbnail_requests_actual_conversion_to_previewed_extension(engine):
    target = Path(engine.settings.output_path) / "Cover.jpg"
    args, _, _, _ = engine.build_command(track(), DownloadOptions.from_dict({"media_type": "thumbnail", "target_path": str(target)}, engine.settings))
    assert args[args.index("--convert-thumbnails") + 1] == "jpg" and "--ffmpeg-location" in args


def test_planned_subtitle_uses_final_previewed_name_after_required_language_suffix(engine):
    target = Path(engine.settings.output_path) / "Caption.srt"
    media = {**track(), "subtitles": {"en": [{"ext": "vtt"}]}}
    def transfer(args, **kwargs):
        target.with_name("Caption.en.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nCaption\n", encoding="utf-8")
        return 0, ""
    with patch("app.downloader.engine.run_process", side_effect=transfer):
        result = engine.download(media, {"media_type": "subtitles", "subtitles": "en", "target_path": str(target)})
    assert str(target) in result["files"] and target.exists() and not target.with_name("Caption.en.srt").exists()
