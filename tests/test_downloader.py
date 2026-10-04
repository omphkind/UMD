import json
import subprocess
from unittest.mock import patch

import pytest

from app.core.config import Settings
from app.core.errors import ConfigurationError, MediaError
from app.downloader.ytdlp import YtDlp, classify_error


@pytest.mark.parametrize("message,reason", [
    ("This video is available to this channel's members", "members_only"),
    ("Join this channel to get access to member-only content", "members_only"),
    ("Private video. Sign in", "private"),
    ("Video has been removed", "deleted"),
    ("Sign in to confirm your age", "age_restricted"),
    ("This video is not available in your country", "geo_restricted"),
    ("Sign in to confirm you're not a bot", "authentication_required"),
    ("No supported JavaScript runtime could be found", "missing_js_runtime"),
    ("Unsupported URL", "unsupported_url"),
    ("Connection timed out", "network"),
    ("Video unavailable", "unavailable"),
    ("Unexpected extractor response", "extractor"),
])
def test_error_classification(message, reason):
    assert classify_error(message) == reason


def backend(tmp_path):
    tool = tmp_path / "yt-dlp.exe"
    tool.touch()
    deno = tmp_path / "deno.exe"
    deno.touch()
    return YtDlp(Settings(yt_dlp_path=str(tool), deno_path=str(deno)), base_dir=tmp_path)


def test_external_process_arguments_are_safe_and_local(tmp_path):
    engine = backend(tmp_path)
    with patch("app.downloader.ytdlp.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps({"id": "abcdefghijk"}), "")
        result = engine.extract("https://youtube.com/watch?v=abcdefghijk&x=a")
        assert result["id"] == "abcdefghijk"
        args = run.call_args.args[0]
        assert args[0] == str(tmp_path / "yt-dlp.exe")
        assert "--ignore-config" in args and "--no-plugin-dirs" in args
        assert "--skip-download" in args and "--no-playlist" in args
        assert args[-2:] == ["--", "https://youtube.com/watch?v=abcdefghijk&x=a"]
        assert f"deno:{tmp_path / 'deno.exe'}" in args
        assert run.call_args.kwargs["shell"] is False
        assert run.call_args.kwargs["timeout"] == 180


def test_partial_flat_result_is_retained(tmp_path):
    engine = backend(tmp_path)
    with patch("app.downloader.ytdlp.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 1, json.dumps({"entries": [{"id": "abcdefghijk"}]}), "Private video")
        assert engine.extract("https://youtube.com/@test", flat=True)["entries"][0]["id"] == "abcdefghijk"
        assert "--flat-playlist" in run.call_args.args[0]


def test_timeout_classified_and_corrupt_json_is_explicit(tmp_path):
    engine = backend(tmp_path)
    with patch("app.downloader.ytdlp.subprocess.run", side_effect=subprocess.TimeoutExpired("yt-dlp", 180)):
        with pytest.raises(MediaError) as error:
            engine.extract("https://youtube.com/watch?v=abcdefghijk")
        assert error.value.reason == "network"
    with patch("app.downloader.ytdlp.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "invalid json", "")):
        with pytest.raises(MediaError) as error:
            engine.extract("https://youtube.com/watch?v=abcdefghijk")
        assert error.value.reason == "extractor"


def test_missing_configured_executable_is_user_error(tmp_path):
    engine = YtDlp(Settings(yt_dlp_path=str(tmp_path / "missing.exe")))
    with pytest.raises(ConfigurationError):
        engine.check()


def test_runtime_version_requires_successful_local_execution(tmp_path):
    engine = backend(tmp_path)
    with patch("app.downloader.ytdlp.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "deno 2.9.7\nv8 14.9", "")) as run:
        assert engine.runtime_version() == "deno 2.9.7"
        assert run.call_args.args[0] == [str(tmp_path / "deno.exe"), "--version"]
        assert run.call_args.kwargs["shell"] is False


def test_missing_runtime_is_classified_without_extracting(tmp_path):
    engine = backend(tmp_path)
    with patch.object(engine, "_executable", return_value=None), patch.object(engine, "_run") as run:
        with pytest.raises(MediaError) as error:
            engine.extract("https://youtube.com/watch?v=abcdefghijk")
        assert error.value.reason == "missing_js_runtime"
        run.assert_not_called()
