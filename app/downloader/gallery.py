"""Download exactly one selected native gallery item with resume and cancellation."""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request

from app.core.errors import ConfigurationError, MediaError
from app.downloader.engine import DownloadOptions, resolve_target, safe_filename
from app.downloader.ffmpeg import check_control, run_process
from app.sources.authentication import AuthenticationManager, public_metadata
from app.sources.gallery_dl_source import GalleryDlSource, image_properties
from app.sources.resolver import validate_url
from app.storage.atomic import write_json


class GalleryDlDownloader:
    def __init__(self, settings, base_dir=None):
        self.settings = settings
        self.service = GalleryDlSource(settings, base_dir=base_dir)
        self.auth = AuthenticationManager(settings)

    def _variant(self, analysis, options):
        variants = analysis.get("photo_formats") or analysis.get("formats") or []
        variants = [entry for entry in variants if isinstance(entry, dict) and entry.get("url") and entry.get("ext")]
        if options.quality not in {"best", "original"}:
            selected = options.quality.removeprefix("format:")
            variants = [entry for entry in variants if entry.get("format_id") == selected]
        if not variants:
            raise MediaError("format_unavailable", "Выбранный вариант изображения недоступен. Повторите анализ.")
        variant = max(variants, key=lambda entry: (int(entry.get("width") or 0) * int(entry.get("height") or 0), int(entry.get("filesize") or 0)))
        ext = variant["ext"].lower()
        if options.media_type == "photo" and options.container not in {"original", ext, "jpeg" if ext == "jpg" else ext}:
            raise MediaError("format_unavailable", "Фото сохраняется в оригинальном формате источника; конвертация в выбранный формат не доступна.")
        return variant

    def _direct_http(self, url, target, variant, control, on_progress):
        partial = target.with_name(target.name + ".part")
        offset = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": "UMD/0.1"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        started = time.monotonic()
        try:
            with self.auth.opener().open(Request(url, headers=headers), timeout=25) as response:
                if offset and response.status == 206:
                    content_range = response.headers.get("Content-Range", "")
                    if not content_range.startswith(f"bytes {offset}-"):
                        raise MediaError("network", "Сервер вернул неверный диапазон; частичный файл сохранён.")
                    mode = "ab"
                else:
                    offset, mode = 0, "wb"
                length = int(response.headers.get("Content-Length") or 0)
                total = offset + length if length else variant.get("filesize") or 0
                transferred = 0
                with partial.open(mode) as output:
                    while True:
                        check_control(control)
                        chunk = response.read(262144)
                        if not chunk:
                            break
                        output.write(chunk)
                        transferred += len(chunk)
                        downloaded = offset + transferred
                        elapsed = max(0.01, time.monotonic() - started)
                        speed = transferred / elapsed
                        if on_progress:
                            on_progress({"stage": "downloading", "downloaded_bytes": downloaded, "total_bytes": total,
                                         "progress": min(99.9, downloaded * 100 / total) if total else 0,
                                         "speed": speed, "eta": max(0, total - downloaded) / speed if total else 0})
                if length and transferred != length:
                    raise MediaError("network", "Файл загружен не полностью. Частичная загрузка сохранена для продолжения.")
        except HTTPError as exc:
            raise MediaError("authentication_required" if exc.code in {401, 403} else "network", f"Источник вернул HTTP {exc.code}.") from None
        except (URLError, OSError) as exc:
            raise MediaError("network", "Не удалось загрузить изображение. Частичный файл сохранён для продолжения.") from exc
        check_control(control)
        return partial

    def _gallery_download(self, url, target, options, variant, on_progress, control):
        # Force the directlink extractor so the original collection is never
        # re-enumerated or downloaded as a side effect of one selected item.
        args = [self.service.executable, "--config-ignore", "--cache-file", ":memory:", "--no-input", "--no-colors", "--quiet",
                "--http-timeout", "25", "--retries", "3", "--directory", str(target.parent),
                "--filename", target.name.replace("{", "{{").replace("}", "}}"),
                "-o", "extractor.skip=false" if options.collision_policy == "overwrite" else "extractor.skip=true",
                *self.auth.arguments(), "--", url]
        partial = target.with_name(target.name + ".part")
        finished = threading.Event()
        total = variant.get("filesize") or 0
        started = time.monotonic()
        previous = partial.stat().st_size if partial.exists() else 0

        def progress():
            while not finished.wait(0.3):
                try:
                    size = partial.stat().st_size if partial.exists() else target.stat().st_size if target.exists() else 0
                except OSError:
                    continue
                if on_progress:
                    speed = max(0, size - previous) / max(0.1, time.monotonic() - started)
                    on_progress({"stage": "downloading", "downloaded_bytes": size, "total_bytes": total,
                                 "progress": min(99.9, size * 100 / total) if total else 0,
                                 "speed": speed, "eta": max(0, total - size) / speed if total and speed else 0})

        watcher = threading.Thread(target=progress, name="umd-gallery-progress", daemon=True)
        watcher.start()
        try:
            code, message = run_process(args, control=control)
        finally:
            finished.set()
            watcher.join(timeout=1)
        if code:
            reason = "authentication_required" if any(token in message.lower() for token in ("401", "403", "cookies", "login")) else "network"
            raise MediaError(reason, self.auth.redact(message[-1200:]) or "gallery-dl не смог загрузить выбранный файл.")
        return target

    def download(self, analysis: dict, options=None, on_progress=None, control=None) -> dict:
        check_control(control)
        selection = options if isinstance(options, DownloadOptions) else DownloadOptions.from_dict(options, self.settings)
        selection = DownloadOptions.from_dict(selection.to_dict(), self.settings)
        if not selection.output_path:
            raise ConfigurationError("Выберите папку для загрузки.")
        if selection.media_type not in {"photo", "video", "audio", "metadata", "thumbnail"}:
            raise MediaError("format_unavailable", "Этот тип загрузки не поддерживается выбранным источником.")
        variant = self._variant(analysis, selection)
        url = validate_url(variant["url"])
        ext = variant["ext"].lower()
        default = Path(selection.output_path) / (safe_filename(analysis.get("title") or "photo") + " [" + safe_filename(str(analysis.get("id") or "media"))[:48] + "]." + ext)
        target = resolve_target(selection, default)
        if selection.media_type != "metadata" and target.suffix.lower().lstrip(".") not in {ext, "jpeg" if ext == "jpg" else ext}:
            raise ConfigurationError("Расширение итогового имени должно совпадать с оригинальным форматом файла.")
        target.parent.mkdir(parents=True, exist_ok=True)
        metadata_path = None
        if selection.metadata_preserve and selection.media_type != "metadata":
            metadata_path = target.with_name(target.name + ".umd.json")
            metadata_path = resolve_target(DownloadOptions.from_dict({**selection.to_dict(), "target_path": str(metadata_path)}), metadata_path)
        if on_progress:
            on_progress({"stage": "preparing", "progress": 0, "speed": 0, "eta": 0})
        if selection.media_type == "metadata":
            if not selection.target_path:
                target = resolve_target(selection, target.with_name(target.stem + ".umd.json"))
            if target.suffix.lower() != ".json":
                raise ConfigurationError("Метаданные сохраняются в JSON.")
            write_json(target, {"analysis": public_metadata(analysis), "download_options": selection.to_dict()})
            files = [str(target)]
        else:
            # CDN URLs with an extension are handled by the real gallery-dl
            # directlink extractor. Opaque media URLs use the same explicit
            # cookie session through the resumable HTTP transport.
            direct_supported = Path(urlparse(url).path).suffix.lower().lstrip(".") in {"jpg", "jpeg", "jpe", "png", "gif", "bmp", "webp", "avif", "heic", "psd", "mp4", "m4v", "mov", "mkv", "ogg", "ogv", "wav", "mp3", "opus"}
            if analysis.get("backend") == "gallery-dl" and direct_supported:
                actual = self._gallery_download(url, target, selection, variant, on_progress, control)
            else:
                actual = self._direct_http(url, target, variant, control, on_progress)
            check_control(control)
            if not actual.is_file() or not actual.stat().st_size:
                raise MediaError("download_empty", "Источник не создал медиафайл.")
            if analysis.get("media_type") == "photo":
                with actual.open("rb") as image_file:
                    actual_ext, _, _ = image_properties(image_file.read(65536))
                canonical_ext = {"jpeg": "jpg", "jpe": "jpg", "tif": "tiff", "heif": "heic"}.get(ext, ext)
                if not actual_ext or actual_ext != canonical_ext:
                    if actual == target:
                        invalid = target.with_name(target.name + ".invalid.part")
                        if invalid.exists():
                            invalid = target.with_name(target.name + f".{time.time_ns()}.invalid.part")
                        os.replace(target, invalid)
                    raise MediaError("invalid_media", "Источник вернул другой тип данных вместо изображения; файл не публикуется как фото.")
            if actual != target:
                if target.exists() and selection.collision_policy != "overwrite":
                    raise MediaError("collision", "Файл появился во время загрузки; частичный результат сохранён.")
                os.replace(actual, target)
            files = [str(target)]
            if metadata_path is not None:
                write_json(metadata_path, {"analysis": public_metadata(analysis), "download_options": selection.to_dict(), "files": files})
                files.append(str(metadata_path))
        if on_progress:
            on_progress({"stage": "completed", "progress": 100, "speed": 0, "eta": 0, "files": files})
        return {"files": files, "metadata": analysis}
