import json
from pathlib import Path
import subprocess

import pytest

from app.core.config import Settings
from app.downloader.ffmpeg import FFmpegProcessor


def test_real_ffmpeg_audio_conversion_and_subtitle_conversion(tmp_path):
    root = Path(__file__).resolve().parents[1]
    if not (root / "tools" / "ffmpeg.exe").is_file():
        pytest.skip("Bundled Windows ffmpeg not installed in this environment")
    processor = FFmpegProcessor(Settings(), base_dir=root)
    assert "ffmpeg version" in processor.check()
    source = tmp_path / "tone.wav"
    processor.run(["-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", str(source)])
    audio = tmp_path / "tone.mp3"
    processor.convert_audio(source, audio, codec="mp3")
    assert audio.stat().st_size > 1000
    source_sub = tmp_path / "subtitle.vtt"
    source_sub.write_text("WEBVTT\n\n00:00.000 --> 00:00.400\nActual subtitles\n", encoding="utf-8")
    target_sub = tmp_path / "subtitle.srt"
    processor.convert_subtitles(source_sub, target_sub)
    assert "Actual subtitles" in target_sub.read_text(encoding="utf-8")
    assert "00:00:00,000" in target_sub.read_text(encoding="utf-8")


def test_real_ffmpeg_merge_streams_and_strip_audio(tmp_path):
    root = Path(__file__).resolve().parents[1]
    if not (root / "tools" / "ffmpeg.exe").is_file():
        pytest.skip("Bundled Windows ffmpeg not installed in this environment")
    processor = FFmpegProcessor(Settings(), base_dir=root)
    video, audio = tmp_path / "video.mp4", tmp_path / "audio.m4a"
    processor.run(["-f", "lavfi", "-i", "color=c=blue:s=128x72:r=10:d=0.5", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)])
    processor.run(["-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", "-c:a", "aac", str(audio)])
    merged = tmp_path / "merged.mp4"
    processor.merge(video, audio, merged)

    def streams():
        result = subprocess.run([str(root / "tools" / "ffprobe.exe"), "-v", "error", "-show_streams", "-of", "json", str(merged)], capture_output=True, text=True, shell=False, check=True)
        return {stream["codec_type"] for stream in json.loads(result.stdout)["streams"]}
    assert streams() == {"video", "audio"}
    processor.remove_audio(merged)
    assert streams() == {"video"}
