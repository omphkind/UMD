"""Choose a backend from analyzed capabilities, never from a site allow-list."""
from app.downloader.engine import YtDlpDownloader
from app.downloader.gallery import GalleryDlDownloader


def create_downloader(settings, analysis: dict, base_dir=None):
    cls = GalleryDlDownloader if analysis.get("backend") in {"gallery-dl", "direct-http"} or analysis.get("media_type") == "photo" else YtDlpDownloader
    return cls(settings, base_dir=base_dir)
