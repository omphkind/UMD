"""Gallery-dl adapter with bounded, incremental enumeration, never full images."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, unquote, urlsplit, urlunsplit
from urllib.request import Request

from app.core.errors import MediaError
from app.downloader.ffmpeg import check_control, run_process
from app.downloader.ytdlp import YtDlp, classify_error
from app.metadata.media import PhotoMetadata
from app.sources.authentication import AuthenticationManager, public_metadata

PHOTO_EXTENSIONS = {"jpg", "jpeg", "jpe", "png", "gif", "webp", "bmp", "tif", "tiff", "avif", "heic", "heif", "psd"}
VIDEO_EXTENSIONS = {"mp4", "m4v", "webm", "mov", "mkv", "ogg", "ogv"}
AUDIO_EXTENSIONS = {"mp3", "m4a", "aac", "wav", "opus", "flac"}


def image_properties(data: bytes) -> tuple[str, int | None, int | None]:
    """Read bounded image headers without decompressing image pixels."""
    width = height = None
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return "png", width, height
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return "gif", width, height
    if data.startswith(b"\xff\xd8\xff"):
        position = 2
        sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while position + 4 <= len(data):
            if data[position] != 0xFF:
                break
            while position < len(data) and data[position] == 0xFF:
                position += 1
            if position >= len(data):
                break
            marker = data[position]
            position += 1
            if marker in {0xD8, 0xD9, 0x01, *range(0xD0, 0xD8)}:
                continue
            if marker == 0xDA or position + 2 > len(data):
                break
            size = int.from_bytes(data[position:position + 2], "big")
            if size < 2:
                break
            if marker in sof and size >= 7 and position + 7 <= len(data):
                height, width = struct.unpack(">HH", data[position + 3:position + 7])
                break
            position += size
        return "jpg", width, height
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8X" and len(data) >= 30:
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
        elif data[12:16] == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            width, height = struct.unpack("<HH", data[26:30])
            width, height = width & 0x3FFF, height & 0x3FFF
        elif data[12:16] == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            width, height = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        return "webp", width, height
    if data.startswith(b"BM"):
        if len(data) >= 26 and int.from_bytes(data[14:18], "little") >= 40:
            width, height = struct.unpack("<ii", data[18:26])
            width, height = abs(width), abs(height)
        return "bmp", width, height
    if data[:4] in {b"II*\x00", b"MM\x00*"}:
        return "tiff", None, None
    if data.startswith(b"8BPS") and len(data) >= 22:
        height, width = struct.unpack(">II", data[14:22])
        return "psd", width, height
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {b"avif", b"avis", b"heic", b"heif", b"mif1"}:
        return "avif" if data[8:12] in {b"avif", b"avis"} else "heic", None, None
    return "", None, None


class _PageComplete(Exception):
    pass


def _author(raw):
    author = raw.get("author") or raw.get("user") or raw.get("uploader") or raw.get("artist") or ""
    if isinstance(author, dict):
        author = author.get("name") or author.get("username") or author.get("screen_name") or author.get("id") or ""
    return str(author)


def normalize_gallery_item(raw: dict, original_url: str, index: int = 1) -> dict:
    """Only backend supplied variants become selectable download formats."""
    from app.sources.resolver import validate_url
    url = validate_url(str(raw.get("download_url") or raw.get("url") or ""))
    ext = str(raw.get("extension") or Path(urlparse(url).path).suffix.lstrip(".")).lower()
    media_type = "photo" if ext in PHOTO_EXTENSIONS else "video" if ext in VIDEO_EXTENSIONS else "audio" if ext in AUDIO_EXTENSIONS else "metadata"
    source = str(raw.get("category") or "gallery")
    # A post can contain multiple images; do not collapse them to the post ID.
    identifier = str(raw.get("id") or raw.get("post_id") or raw.get("media_id") or "")
    parts = urlsplit(url)
    digest = hashlib.sha256(urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")).encode()).hexdigest()[:16]
    identifier = f"{identifier}-{raw.get('num') or raw.get('index') or digest}" if identifier else digest
    filename = str(raw.get("filename") or Path(unquote(urlparse(url).path)).stem or "photo")
    title = str(raw.get("title") or filename)
    width = raw.get("width") or raw.get("image_width")
    height = raw.get("height") or raw.get("image_height")
    original_filename = filename if Path(filename).suffix.lower() == "." + ext else filename + "." + ext
    variant = {"format_id": "original", "url": url, "ext": ext, "width": width, "height": height, "filesize": raw.get("filesize") or raw.get("file_size")}
    formats = [variant] if media_type != "metadata" else []
    safe_raw = public_metadata(raw)
    photo = PhotoMetadata(title=title, original_title=title, author=_author(raw), source=source,
                          source_id=identifier, published_at=str(raw.get("date") or raw.get("created_at") or ""),
                          description=str(raw.get("description") or raw.get("content") or ""), tags=raw.get("tags") or [],
                          width=width, height=height, format=ext, original_filename=original_filename,
                          gallery_name=str(raw.get("album") or raw.get("gallery") or raw.get("collection") or ""),
                          gallery_index=index, source_metadata=safe_raw).to_dict()
    return {"id": identifier, "media_id": identifier, "source_id": identifier,
            "source": source, "extractor": "gallery-dl:" + source, "backend": "gallery-dl",
            "url": url, "input_url": original_url, "source_url": original_url, "download_url": url,
            "media_type": media_type, "title": title, "original_title": title, "title_source": "original",
            "description": photo["description"], "original_description": photo["description"], "description_source": "original",
            "author": photo["author"], "published_at": photo["published_at"], "duration": raw.get("duration"),
            "thumbnail": str(raw.get("thumbnail") or raw.get("thumbnail_url") or (url if media_type == "photo" else "")),
            "dimensions": {"width": width, "height": height}, "width": width, "height": height,
            "original_filename": original_filename, "filesize": variant.get("filesize"),
            "formats": formats, "photo_formats": formats if media_type == "photo" else [],
            "video_formats": [{**f, "vcodec": "unknown", "acodec": "unknown"} for f in formats] if media_type == "video" else [],
            "audio_formats": [{**f, "vcodec": "none", "acodec": "unknown"} for f in formats] if media_type == "audio" else [],
            "qualities": [], "subtitles": {}, "automatic_captions": {}, "subtitle_languages": [], "chapters": [],
            "entries": [], "needs_analysis": False, "is_shorts": False, "is_members_only": False,
            "collection_type": "single", "collection_index": index, "gallery_index": index,
            "availability": "public", "capabilities": ["metadata", media_type] if formats else ["metadata"],
            "capability_status": "supported" if formats else "metadata_only",
            "capability_reason": "" if formats else "Этот тип файла не поддерживается загрузчиком.",
            "downloadable": bool(formats), "metadata": photo, "source_metadata": safe_raw}


class GalleryDlSource:
    def __init__(self, settings, base_dir=None):
        self.settings = settings
        self.tools = YtDlp(settings, base_dir=base_dir)
        self.auth = AuthenticationManager(settings)

    @property
    def executable(self):
        return self.tools._executable(getattr(self.settings, "gallery_dl_path", ""), "gallery-dl")

    def check(self):
        code, text = run_process([self.executable, "--version"], timeout=20)
        if code or not text.strip():
            raise MediaError("missing_gallery_dl", "gallery-dl не запускается. Проверьте инструмент в настройках.")
        return text.strip().splitlines()[-1]

    def supports(self, url: str, control=None) -> bool:
        code, _ = run_process([self.executable, "--config-ignore", "--cache-file", ":memory:", "--no-input", "--extractor-info", "--", url],
                              control=control, timeout=20)
        return code == 0

    def analyze(self, url: str, control=None, page: int = 1, page_size: int = 80):
        from app.sources.resolver import validate_url
        url = validate_url(url)
        if not isinstance(page, int) or not isinstance(page_size, int) or page < 1 or not 1 <= page_size <= 200:
            raise MediaError("invalid_page", "Размер страницы галереи должен быть от 1 до 200.")
        offset = (page - 1) * page_size
        entries, seen = [], 0
        more = False

        def record(line):
            nonlocal seen, more
            if not line.startswith("{"):
                return
            try:
                raw = json.loads(line)
            except ValueError:
                return
            if not isinstance(raw, dict) or not raw.get("download_url"):
                return
            seen += 1
            if seen <= offset:
                return
            if len(entries) == page_size:
                more = True
                raise _PageComplete()
            entries.append(normalize_gallery_item(raw, url, seen))

        # DownloadJob's metadata postprocessor prints each file independently.
        # DataJob (-j/-J) retains all results in memory even in JSONL mode.
        postprocessor = json.dumps([{"name": "metadata", "mode": "jsonl", "filename": "-", "event": "prepare"}])
        with tempfile.TemporaryDirectory(prefix="umd-gallery-") as temporary:
            args = [self.executable, "--config-ignore", "--cache-file", ":memory:", "--no-input", "--no-colors", "--no-download", "--quiet",
                    "--http-timeout", "25", "--retries", "2", "--directory", temporary,
                    "-o", "extractor.skip=false", "-o", "extractor.metadata-url=download_url",
                    "-o", "extractor.postprocessors=" + postprocessor, *self.auth.arguments(), "--", url]
            try:
                code, message = run_process(args, on_line=record, control=control, timeout=180)
            except _PageComplete:
                code, message = 0, ""
        check_control(control)
        partial_reason = self.auth.redact(message[-1200:]) if code and entries else ""
        if not entries:
            reason = classify_error(message)
            if "unsupported" in message.lower() or "no suitable extractor" in message.lower():
                reason = "unsupported_url"
            if any(value in message.lower() for value in ("authentication", "login", "cookies", "401", "403")):
                reason = "authentication_required"
            raise MediaError(reason, self.auth.redact(message[-1200:]) or "gallery-dl не вернул доступные элементы.")
        first = entries[0]
        collection_id = hashlib.sha256(url.encode()).hexdigest()[:20]
        collection_title = first["metadata"].get("gallery_name") or first.get("author") or first["source"]
        subtype = str(first["source_metadata"].get("subcategory") or "gallery")
        collection_type = subtype if subtype in {"album", "gallery", "playlist", "channel", "profile", "feed"} else "profile" if subtype in {"user", "account", "board"} else "gallery"
        for item in entries:
            item.update(collection_id=collection_id, collection=collection_title, collection_name=collection_title, collection_type=collection_type)
        if len(entries) == 1 and page == 1 and not more and subtype not in {"album", "gallery", "playlist", "channel", "profile", "feed", "user", "account", "board"}:
            return {**first, "has_more": False, "page": 1, "collection_type": "single",
                    "capability_status": "supported_with_limitations" if partial_reason else first["capability_status"],
                    "capability_reason": partial_reason}
        media_types = sorted({entry["media_type"] for entry in entries})
        return {**first, "id": collection_id, "media_id": collection_id, "url": url, "input_url": url,
                "title": collection_title, "original_title": collection_title, "media_type": "playlist", "entries": entries,
                "formats": [], "photo_formats": [], "audio_formats": [], "video_formats": [],
                "collection_id": collection_id, "collection_type": collection_type, "collection_name": collection_title,
                "collection_media_types": media_types, "capabilities": ["metadata", "playlist", *media_types],
                "capability_status": "supported_with_limitations" if partial_reason else "supported",
                "capability_reason": partial_reason,
                "has_more": more, "next_page": page + 1 if more else None, "page": page, "page_size": page_size,
                "entries_count": len(entries), "total_count": None if more or partial_reason else seen, "needs_analysis": False}


def direct_photo(url: str, settings, control=None) -> dict:
    """Verify content and inspect only its first 64 KiB; transfer happens later."""
    check_control(control)
    try:
        with AuthenticationManager(settings).opener().open(Request(url, headers={"User-Agent": "UMD/0.1", "Accept": "image/*"}), timeout=25) as response:
            content_type = response.headers.get_content_type()
            data = response.read(65536)
            actual_url = response.geturl()
            length = response.headers.get("Content-Length")
    except HTTPError as exc:
        raise MediaError("authentication_required" if exc.code in {401, 403} else "network", f"Фото недоступно: HTTP {exc.code}.") from None
    except (URLError, OSError) as exc:
        raise MediaError("network", "Не удалось получить фото. Проверьте подключение и доступность ссылки.") from exc
    check_control(control)
    ext, width, height = image_properties(data)
    if not ext or (not content_type.startswith("image/") and content_type not in {"application/octet-stream", "binary/octet-stream"}):
        raise MediaError("unsupported_url", "Ссылка не возвращает поддерживаемый файл изображения.")
    try:
        size = int(length) if length else None
    except ValueError:
        size = None
    raw = {"download_url": actual_url, "category": "direct", "extension": ext,
           "filename": Path(unquote(urlparse(url).path)).stem or "photo", "width": width, "height": height, "filesize": size}
    return {**normalize_gallery_item(raw, url), "backend": "direct-http", "source_url": url, "collection_type": "single", "has_more": False}
