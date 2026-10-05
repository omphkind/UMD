import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.config import Settings
from app.core.errors import ConfigurationError, MediaError
from app.downloader.engine import DownloadOptions, DownloadInterrupted, YtDlpDownloader, parse_progress, subtitle_languages
from app.downloader.ffmpeg import run_process


def settings(tmp_path):
    configured = Settings(output_path=str(tmp_path / "downloads"), localization=False)
    configured.yt_dlp_path = str(tmp_path / "yt-dlp.exe")
    configured.deno_path = str(tmp_path / "deno.exe")
    configured.ffmpeg_path = str(tmp_path / "ffmpeg.exe")
    for path in (configured.yt_dlp_path, configured.deno_path, configured.ffmpeg_path):
        Path(path).touch()
    return configured


def analysis():
    return {"id": "media1", "url": "https://media.example/video", "source": "example", "title": "Example [title]",
            "formats": [
                {"format_id": "v1080", "ext": "mp4", "height": 1080, "fps": 30, "vcodec": "avc1", "acodec": "none"},
                {"format_id": "v720", "ext": "mp4", "height": 720, "vcodec": "avc1", "acodec": "none"},
                {"format_id": "audio", "ext": "m4a", "vcodec": "none", "acodec": "mp4a", "abr": 128},
            ], "subtitles": {"ru": [{"ext": "vtt"}]}, "automatic_captions": {"en": [{"ext": "vtt"}]},
            "thumbnail": "https://media.example/thumb.jpg", "chapters": [{"title": "Intro", "start_time": 0, "end_time": 10}]}


def test_video_merge_uses_real_ids_and_configured_ffmpeg(tmp_path):
    configured = settings(tmp_path)
    engine = YtDlpDownloader(configured)
    command, output, prefix, strip = engine.build_command(analysis(), DownloadOptions.from_dict({"quality": "1080", "chapters": False}, configured))
    assert command[command.index("--format") + 1] == "v1080+audio"
    assert command[command.index("--ffmpeg-location") + 1] == configured.ffmpeg_path
    assert "--continue" in command and "--part" in command and "--ignore-config" in command
    assert command[-2:] == ["--", "https://media.example/video"]
    assert output == (tmp_path / "downloads").resolve()
    assert strip is False


def test_nonexistent_quality_is_error_instead_of_silent_downgrade(tmp_path):
    configured = settings(tmp_path)
    with pytest.raises(MediaError) as error:
        YtDlpDownloader(configured).build_command(analysis(), DownloadOptions.from_dict({"quality": "2160"}, configured))
    assert error.value.reason == "format_unavailable"


def test_webm_to_mp4_recode_and_audio_only_options(tmp_path):
    configured = settings(tmp_path)
    media = analysis()
    media["formats"][0].update(ext="webm", vcodec="vp9")
    command, _, _, _ = YtDlpDownloader(configured).build_command(media, DownloadOptions.from_dict({"quality": "1080"}, configured))
    assert "--recode-video" in command
    assert command[command.index("--merge-output-format") + 1] == "mkv"
    command, _, _, _ = YtDlpDownloader(configured).build_command(media, DownloadOptions.from_dict({"media_type": "audio", "container": "mp3"}, configured))
    assert command[command.index("--format") + 1] == "audio"
    assert command[command.index("--audio-format") + 1] == "mp3"


def test_advanced_audio_choice_exact_format_id_is_honored(tmp_path):
    configured = settings(tmp_path)
    media = analysis()
    media["formats"].append({"format_id": "highaudio", "ext": "m4a", "vcodec": "none", "acodec": "mp4a", "abr": 256})
    engine = YtDlpDownloader(configured)
    command, _, _, _ = engine.build_command(media, DownloadOptions.from_dict({"media_type": "audio", "container": "mp3", "quality": "format:audio"}, configured))
    assert command[command.index("--format") + 1] == "audio"
    with pytest.raises(MediaError):
        engine.build_command(media, DownloadOptions.from_dict({"media_type": "audio", "container": "mp3", "quality": "format:missing"}, configured))


def test_video_only_combined_requires_audio_removal(tmp_path):
    configured = settings(tmp_path)
    media = analysis()
    media["formats"] = [{"format_id": "combined", "ext": "mp4", "height": 360, "vcodec": "avc1", "acodec": "mp4a"}]
    command, _, _, strip = YtDlpDownloader(configured).build_command(media, DownloadOptions.from_dict({"audio": "video_only"}, configured))
    assert strip
    assert command[command.index("--format") + 1] == "combined"


def test_subtitles_select_actual_languages_including_auto_and_convert(tmp_path):
    configured = settings(tmp_path)
    command, _, _, _ = YtDlpDownloader(configured).build_command(analysis(), DownloadOptions.from_dict({"media_type": "subtitles", "subtitles": "ru", "subtitle_format": "ass"}, configured))
    assert "--skip-download" in command and "--write-auto-subs" in command
    assert command[command.index("--sub-langs") + 1] == "ru"
    assert command[command.index("--convert-subs") + 1] == "ass"
    assert subtitle_languages(analysis(), "all") == ["en", "ru"]
    with pytest.raises(MediaError):
        subtitle_languages(analysis(), "de")


def test_subtitle_mode_cannot_succeed_with_no_selected_language(tmp_path):
    configured = settings(tmp_path)
    with pytest.raises(ConfigurationError):
        YtDlpDownloader(configured).build_command(analysis(), DownloadOptions.from_dict({"media_type": "subtitles"}, configured))


def test_metadata_mode_really_writes_atomic_json_and_chapters_without_download(tmp_path):
    configured = settings(tmp_path)
    events = []
    with patch("app.downloader.engine.run_process") as run:
        result = YtDlpDownloader(configured).download(analysis(), {"media_type": "metadata"}, events.append)
    run.assert_not_called()
    assert len(result["files"]) == 2
    payload = json.loads(Path(result["files"][0]).read_text(encoding="utf-8"))
    assert payload["analysis"]["id"] == "media1"
    assert json.loads(Path(result["files"][1]).read_text())["chapters"][0]["title"] == "Intro"
    assert events[-1]["stage"] == "completed"


def test_thumbnail_files_with_brackets_are_found_and_partial_files_ignored(tmp_path):
    configured = settings(tmp_path)
    engine = YtDlpDownloader(configured)
    _, output, prefix, _ = engine.build_command(analysis(), DownloadOptions.from_dict({"media_type": "thumbnail"}, configured))

    def run(*args, **kwargs):
        (output / (prefix + ".jpg")).write_bytes(b"image")
        (output / (prefix + ".mp4.part")).write_bytes(b"partial")
        return 0, ""
    with patch("app.downloader.engine.run_process", side_effect=run):
        result = engine.download(analysis(), {"media_type": "thumbnail", "chapters": False})
    assert any(path.endswith(".jpg") for path in result["files"])
    assert not any(path.endswith(".part") for path in result["files"])


def test_failed_download_preserves_partial_and_reports_error(tmp_path):
    configured = settings(tmp_path)
    engine = YtDlpDownloader(configured)
    _, output, prefix, _ = engine.build_command(analysis(), DownloadOptions.from_dict({}, configured))
    partial = output / (prefix + ".mp4.part")

    def run(*args, **kwargs):
        partial.write_bytes(b"keep for retry")
        return 1, "Connection timed out"
    with patch("app.downloader.engine.run_process", side_effect=run):
        with pytest.raises(MediaError) as error:
            engine.download(analysis(), {})
    assert error.value.reason == "network"
    assert partial.read_bytes() == b"keep for retry"


def test_progress_is_machine_readable_and_keeps_units():
    event = parse_progress('UMD_PROGRESS:{"downloaded_bytes":50,"total_bytes":200,"speed":1024,"eta":3}')
    assert event["progress"] == 25
    assert event["speed"] == 1024 and event["eta"] == 3
    assert parse_progress("[download] ordinary text") is None
    assert parse_progress("UMD_PROGRESS:null") is None


@pytest.mark.parametrize("action,reason", [("pause", "paused"), ("cancel", "cancelled")])
def test_silent_child_is_interruptible_and_partial_preserved(tmp_path, action, reason):
    partial = tmp_path / "download.part"
    script = tmp_path / "child.py"
    script.write_text("from pathlib import Path\nimport time\nPath(" + repr(str(partial)) + ").write_bytes(b'partial')\ntime.sleep(30)\n", encoding="utf-8")
    control = SimpleNamespace(pause=threading.Event(), cancel=threading.Event())
    timer = threading.Timer(0.4, getattr(control, action).set)
    timer.start()
    try:
        with pytest.raises(DownloadInterrupted) as error:
            run_process([sys.executable, str(script)], control=control)
        assert error.value.reason == reason
        assert partial.read_bytes() == b"partial"
    finally:
        timer.cancel()
