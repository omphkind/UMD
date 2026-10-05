"""Real local extractor/download/postprocessor integration, without external sites."""
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading

import pytest

from app.core.config import Settings
from app.downloader.engine import YtDlpDownloader
from app.downloader.ffmpeg import FFmpegProcessor
from app.sources.resolver import SourceResolver


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def test_real_http_extract_video_audio_thumbnail_subtitles_and_video_only(tmp_path):
    root = Path(__file__).resolve().parents[1]
    if not (root / "tools" / "ffmpeg.exe").is_file() or not (root / "tools" / "yt-dlp.exe").is_file():
        pytest.skip("Bundled Windows tools unavailable")
    settings = Settings(output_path=str(tmp_path / "downloads"), localization=False)
    processor = FFmpegProcessor(settings, base_dir=root)
    web = tmp_path / "web"
    web.mkdir()
    sample = web / "sample.mp4"
    processor.run(["-f", "lavfi", "-i", "color=c=blue:s=128x72:r=10:d=1", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(sample)])
    processor.run(["-i", str(sample), "-frames:v", "1", str(web / "poster.png")])
    (web / "subtitle.vtt").write_text("WEBVTT\n\n00:00.000 --> 00:00.900\nActual subtitle fixture\n", encoding="utf-8")
    (web / "index.html").write_text('<html><head><title>Actual local video</title></head><body><video controls poster="poster.png"><source src="sample.mp4" type="video/mp4"><track kind="subtitles" srclang="en" src="subtitle.vtt"></video></body></html>', encoding="utf-8")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(web)))
    except OSError as exc:
        pytest.skip("Local HTTP bind unavailable: " + str(exc))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        resolver, engine = SourceResolver(settings), YtDlpDownloader(settings)
        direct = resolver.analyze(base + "/sample.mp4")
        assert direct["source"] == "generic" and direct["formats"]
        events = []
        video = engine.download(direct, {"container": "original", "chapters": False}, events.append)
        downloaded = next(Path(path) for path in video["files"] if path.endswith(".mp4"))
        assert hashlib.sha256(downloaded.read_bytes()).digest() == hashlib.sha256(sample.read_bytes()).digest()
        assert events[-1]["stage"] == "completed"
        assert any(event["stage"] == "downloading" for event in events)
        audio = engine.download(direct, {"media_type": "audio", "container": "mp3", "chapters": False})
        assert any(Path(path).stat().st_size > 1000 for path in audio["files"] if path.endswith(".mp3"))
        silent = engine.download(direct, {"container": "original", "audio": "video_only", "chapters": False})
        silent_file = next(path for path in silent["files"] if path.endswith(".mp4"))
        streams = subprocess.run([str(root / "tools" / "ffprobe.exe"), "-v", "error", "-show_streams", "-of", "json", silent_file],
                                 capture_output=True, text=True, check=True, shell=False)
        assert {stream["codec_type"] for stream in json.loads(streams.stdout)["streams"]} == {"video"}
        rich = resolver.analyze(base + "/index.html")
        if rich["media_type"] == "playlist":
            rich = rich["entries"][0]
        assert rich["thumbnail"]
        assert "en" in rich["subtitles"]
        thumbnail = engine.download(rich, {"media_type": "thumbnail", "chapters": False})
        assert any(path.endswith(".png") and Path(path).stat().st_size > 0 for path in thumbnail["files"])
        subtitles = engine.download(rich, {"media_type": "subtitles", "subtitles": "en", "subtitle_format": "srt", "chapters": False})
        subtitle = next(Path(path) for path in subtitles["files"] if path.endswith(".srt"))
        assert "Actual subtitle fixture" in subtitle.read_text(encoding="utf-8")
        second = web / "second.mp4"
        processor.run(["-f", "lavfi", "-i", "color=c=red:s=128x72:r=10:d=0.5", "-f", "lavfi", "-i", "sine=frequency=880:duration=0.5",
                       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(second)])
        (web / "multiple.html").write_text('<html><head><title>Two different videos</title></head><body><video src="sample.mp4"></video><video src="second.mp4"></video></body></html>', encoding="utf-8")
        multiple = resolver.analyze(base + "/multiple.html")
        assert len(multiple["entries"]) == 2
        result = engine.download(multiple["entries"][1], {"container": "original", "chapters": False})
        selected = next(Path(path) for path in result["files"] if path.endswith(".mp4"))
        assert hashlib.sha256(selected.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
