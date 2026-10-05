import base64
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.errors import MediaError, SkipItem
from app.downloader.ffmpeg import DownloadInterrupted
from app.downloader.gallery import GalleryDlDownloader
from app.sources.gallery_dl_source import GalleryDlSource, normalize_gallery_item
from app.sources.resolver import SourceResolver


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jqZkAAAAASUVORK5CYII=")


@pytest.fixture
def pictures(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "photo.png").write_bytes(PNG + b"\x00" * 900000)
    (web / "not-photo.png").write_text("<html>Authentication required</html>")
    requests = []
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            requests.append(self.headers.get("Range"))
            if self.path == "/photo.png" and self.headers.get("Range"):
                data = (web / "photo.png").read_bytes()
                offset = int(self.headers["Range"].partition("=")[2].partition("-")[0])
                self.send_response(206)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data) - offset))
                self.send_header("Content-Range", f"bytes {offset}-{len(data)-1}/{len(data)}")
                self.end_headers()
                self.wfile.write(data[offset:])
            else:
                super().do_GET()
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(web)))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{server.server_port}", web, requests
    server.shutdown()
    server.server_close()
    worker.join(3)


def test_real_photo_header_analysis_and_pause_resume_in_named_folder(tmp_path, pictures):
    base, web, requests = pictures
    settings = Settings(output_path=str(tmp_path / "downloads"), localization=False)
    analysis = SourceResolver(settings).analyze(base + "/photo.png")
    assert analysis["media_type"] == "photo" and analysis["width"] == analysis["height"] == 1
    target = Path(settings.output_path) / "Фото" / "Отпуск - 001.png"
    control = SimpleNamespace(pause=threading.Event(), cancel=threading.Event())
    def progress(event):
        if event.get("downloaded_bytes"):
            control.pause.set()
    options = {"media_type": "photo", "container": "original", "quality": "original", "target_path": str(target)}
    engine = GalleryDlDownloader(settings)
    with pytest.raises(DownloadInterrupted) as error:
        engine.download(analysis, options, progress, control)
    assert error.value.reason == "paused" and not target.exists()
    partial_file = target.with_name(target.name + ".part")
    assert partial_file.exists() and partial_file.stat().st_size > 0
    result = engine.download(analysis, options)
    assert requests[-1].startswith("bytes=")
    assert target.read_bytes() == (web / "photo.png").read_bytes()
    assert not partial_file.exists()
    assert json.loads(Path(result["files"][1]).read_text(encoding="utf-8"))["analysis"]["media_type"] == "photo"


def test_actual_gallery_dl_enumeration_and_exact_selected_file(tmp_path, pictures):
    base, web, _ = pictures
    root = Path(__file__).resolve().parents[1]
    if not (root / "tools" / "gallery-dl.exe").is_file():
        pytest.skip("Bundled gallery-dl unavailable")
    settings = Settings(output_path=str(tmp_path / "downloads"), localization=False)
    source = GalleryDlSource(settings)
    assert source.supports(base + "/photo.png")
    analysis = source.analyze(base + "/photo.png")
    assert analysis["backend"] == "gallery-dl" and analysis["photo_formats"][0]["ext"] == "png"
    target = Path(settings.output_path) / "Selected - 005.png"
    result = GalleryDlDownloader(settings).download(analysis, {"media_type": "photo", "container": "original", "target_path": str(target)})
    assert target.read_bytes() == (web / "photo.png").read_bytes()
    assert len(result["files"]) == 2


@pytest.mark.parametrize("policy", ["ask", "skip", "append_number", "overwrite"])
def test_photo_collision_policies_protect_existing_bytes(tmp_path, pictures, policy):
    base, web, _ = pictures
    settings = Settings(output_path=str(tmp_path / "downloads"), localization=False)
    target = Path(settings.output_path) / "Existing.png"
    target.parent.mkdir()
    target.write_bytes(b"user file")
    analysis = SourceResolver(settings).analyze(base + "/photo.png")
    engine = GalleryDlDownloader(settings)
    options = {"media_type": "photo", "container": "original", "target_path": str(target), "collision_policy": policy}
    if policy in {"ask", "skip"}:
        with pytest.raises(MediaError if policy == "ask" else SkipItem):
            engine.download(analysis, options)
        assert target.read_bytes() == b"user file"
    else:
        result = engine.download(analysis, options)
        actual = Path(result["files"][0])
        assert actual.read_bytes() == (web / "photo.png").read_bytes()
        if policy == "append_number":
            assert actual.name == "Existing (1).png" and target.read_bytes() == b"user file"


def test_html_login_page_is_not_reported_as_photo(tmp_path, pictures):
    base, _, _ = pictures
    settings = Settings(output_path=str(tmp_path / "downloads"))
    with pytest.raises(MediaError):
        SourceResolver(settings).analyze(base + "/not-photo.png")
    # Also protect against source bytes changing after a successful analysis.
    analysis = normalize_gallery_item({"category": "direct", "extension": "png", "filename": "Photo", "download_url": base + "/not-photo.png"}, base)
    analysis["backend"] = "direct-http"
    with pytest.raises(MediaError) as error:
        GalleryDlDownloader(settings).download(analysis, {"media_type": "photo", "container": "original"})
    assert error.value.reason == "invalid_media"
    assert not list(Path(settings.output_path).glob("*.png"))


def test_gallery_metadata_preserves_planned_json_name_without_transfer(tmp_path):
    settings = Settings(output_path=str(tmp_path / "downloads"))
    analysis = normalize_gallery_item({"category": "photos", "extension": "png", "filename": "Photo", "download_url": "https://example.org/image.png"}, "https://example.org/post")
    target = Path(settings.output_path) / "metadata.json"
    result = GalleryDlDownloader(settings).download(analysis, {"media_type": "metadata", "target_path": str(target)})
    assert result["files"] == [str(target)] and json.loads(target.read_text(encoding="utf-8"))["analysis"]["media_type"] == "photo"
