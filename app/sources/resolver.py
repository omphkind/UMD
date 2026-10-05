"""Resolve real yt-dlp extractors instead of maintaining a site allow-list."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import json
import logging
from urllib.parse import urlparse

from app.core.errors import MediaError
from app.downloader.ytdlp import YtDlp, classify_error
from app.downloader.ffmpeg import check_control, run_process, DownloadInterrupted
from app.metadata.cleanup import clean_description, clean_title
from app.sources.youtube import parse_youtube_url, shorts_signals, is_members_only, video_url, VIDEO_ID

log = logging.getLogger(__name__)
VIDEO_EXTENSIONS = {"mp4", "mkv", "webm", "mov", "avi", "flv", "m4v", "ts", "m3u8"}
AUDIO_EXTENSIONS = {"mp3", "m4a", "aac", "opus", "ogg", "wav", "flac"}


def validate_url(url: str) -> str:
    if not isinstance(url, str):
        raise MediaError("unsupported_url", "Введите URL медиаресурса.")
    url = url.strip()
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or any(ch in url for ch in "\r\n\x00"):
            raise ValueError
        parsed.port
    except ValueError:
        raise MediaError("unsupported_url", "Нужен корректный HTTP/HTTPS URL медиаресурса без пароля.") from None
    return url


def has_video(fmt: dict) -> bool:
    value = fmt.get("vcodec")
    return value != "none" and (bool(value) or fmt.get("ext") in VIDEO_EXTENSIONS)


def has_audio(fmt: dict) -> bool:
    value = fmt.get("acodec")
    return value != "none" and (bool(value) or fmt.get("ext") in AUDIO_EXTENSIONS | VIDEO_EXTENSIONS)


@dataclass(frozen=True)
class DetectedSource:
    name: str
    extractor: str
    media_type: str
    capabilities: list[str]
    availability: str


class SourceResolver:
    def __init__(self, settings, backend=None, localizer=None):
        self.settings = settings
        self.backend = backend or YtDlp(settings)
        self.localizer = localizer

    def _extract(self, url: str, *, flat: bool, control=None) -> dict:
        if control is None or not isinstance(self.backend, YtDlp):
            check_control(control)
            return self.backend.extract(url, flat=flat)
        executable = self.backend._executable(self.settings.yt_dlp_path, "yt-dlp")
        args = [executable, "--ignore-config", "--no-plugin-dirs", "--no-remote-components", "--skip-download", "--dump-single-json",
                "--encoding", "utf-8", "--socket-timeout", "25", "--retries", "2", "--extractor-retries", "2", "--no-progress", "--no-warnings"]
        deno = self.backend._executable(getattr(self.settings, "deno_path", ""), "deno", required=False)
        if deno:
            args.extend(["--js-runtimes", "deno:" + deno])
        args.extend(["--flat-playlist", "--ignore-errors"] if flat else ["--no-playlist"])
        payload = []

        def read(line):
            if line.startswith("{"):
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, dict):
                        payload.append(parsed)
                except json.JSONDecodeError:
                    pass
        code, message = run_process([*args, "--", url], on_line=read, control=control, timeout=180)
        if code and not (flat and payload):
            raise MediaError(classify_error(message), message[-1600:] or "Не удалось проанализировать URL.")
        if not payload:
            raise MediaError("extractor", "yt-dlp не вернул корректные метаданные.")
        return payload[-1]

    def analyze(self, url: str, control=None) -> dict:
        check_control(control)
        url = validate_url(url)
        try:
            youtube = parse_youtube_url(url)
        except MediaError:
            youtube = None
        if youtube and youtube.kind == "video":
            raw = self._extract(youtube.url, flat=False, control=control)
        else:
            raw = self._extract(url, flat=True, control=control)
        check_control(control)
        analysis = self._normalize(raw, url)
        if youtube and youtube.is_shorts:
            analysis["is_shorts"] = True
        if analysis["source"] == "youtube" and analysis["media_type"] != "playlist" and self.settings.localization:
            try:
                if self.localizer:
                    localized = self.localizer.fetch(analysis["url"])
                else:
                    # Worker owns its browser lifecycle; never share sync pages
                    # with another GUI thread or retain a closed context.
                    from app.localization.youtube import YouTubeLocalizer
                    with YouTubeLocalizer(self.settings) as browser:
                        localized = browser.fetch(analysis["url"])
                if localized.get("title"):
                    analysis["title"] = clean_title(localized["title"])
                    analysis["title_source"] = "youtube_localized"
                if localized.get("description_found", bool(localized.get("description"))):
                    analysis["description"] = clean_description(localized.get("description", ""))
                    analysis["description_source"] = "youtube_localized"
                analysis["localized_values"] = {self.settings.locale: localized}
            except DownloadInterrupted:
                raise
            except Exception as exc:
                log.warning("YouTube localization fallback: %s", exc)
        check_control(control)
        return analysis

    def _normalize(self, raw: dict, original_url: str, *, inherited_source: str = "", source_tab: str = "") -> dict:
        extractor = str(raw.get("extractor_key") or raw.get("extractor") or inherited_source or "generic")
        source = "youtube" if extractor.lower().startswith("youtube") else extractor.lower().split(":")[0]
        url = str(raw.get("webpage_url") or raw.get("original_url") or raw.get("url") or original_url)
        media_id = str(raw.get("id") or "")
        if source == "youtube" and VIDEO_ID.fullmatch(media_id):
            url = video_url(media_id)
        formats = [dict(fmt) for fmt in raw.get("formats", []) if isinstance(fmt, dict) and fmt.get("format_id") and not fmt.get("has_drm")]
        # Some extractors return one real direct format outside `formats`.
        if not formats and not raw.get("formats") and not raw.get("has_drm") and raw.get("url") and raw.get("ext") and raw.get("entries") is None and raw.get("_type") not in {"url", "url_transparent"}:
            formats = [{key: raw.get(key) for key in ("format_id", "ext", "url", "width", "height", "fps", "vcodec", "acodec", "tbr", "abr", "filesize", "protocol")}]
            formats[0]["format_id"] = str(raw.get("format_id") or "0")
        available_video = [fmt for fmt in formats if has_video(fmt)]
        available_audio = [fmt for fmt in formats if has_audio(fmt)]
        entries = []
        seen = set()
        current_tab = "shorts" if url.rstrip("/").endswith("/shorts") else source_tab
        for index, entry in enumerate(raw.get("entries") or [], 1):
            if not isinstance(entry, dict):
                continue
            normalized = self._normalize(entry, url, inherited_source=source, source_tab=current_tab)
            normalized["playlist_url"] = url
            normalized["playlist_index"] = entry.get("playlist_index") or index
            candidates = normalized["entries"] if normalized["media_type"] == "playlist" else [normalized]
            for candidate in candidates:
                identity = (candidate["source"], candidate["id"] or candidate["url"])
                if identity not in seen:
                    seen.add(identity)
                    entries.append(candidate)
        playlist = raw.get("entries") is not None
        subtitles = raw.get("subtitles") or {}
        automatic = raw.get("automatic_captions") or {}
        thumbnails = raw.get("thumbnails") or []
        thumbnail = raw.get("thumbnail") or (thumbnails[-1].get("url", "") if thumbnails else "")
        capabilities = ["metadata"]
        if available_video:
            capabilities.append("video")
        if available_audio:
            capabilities.append("audio")
        if subtitles or automatic:
            capabilities.append("subtitles")
        if thumbnail:
            capabilities.append("thumbnail")
        if raw.get("chapters"):
            capabilities.append("chapters")
        if playlist:
            capabilities.append("playlist")
        media_type = "playlist" if playlist else "video" if available_video else "audio" if available_audio else "metadata"
        evidence = {**raw, "source_tab": current_tab}
        detected = DetectedSource(source, extractor, media_type, capabilities, str(raw.get("availability") or "public"))
        title = clean_title(raw.get("title"))
        description = str(raw.get("description") or "")
        return {
            "id": media_id, "media_id": media_id, "url": url, "input_url": original_url,
            "source": source, "extractor": extractor, "media_type": media_type,
            "detected_source": asdict(detected), "capabilities": capabilities,
            "availability": detected.availability, "title": title, "original_title": title,
            "title_source": "original", "description": clean_description(description),
            "original_description": description, "description_source": "original",
            "author": raw.get("channel") or raw.get("uploader") or raw.get("creator") or "",
            "duration": raw.get("duration"), "published_at": raw.get("upload_date") or raw.get("release_date") or "",
            "thumbnail": thumbnail, "thumbnails": thumbnails,
            "formats": formats, "video_formats": available_video, "audio_formats": available_audio,
            "qualities": sorted({int(f["height"]) for f in available_video if isinstance(f.get("height"), (int, float)) and f["height"] > 0}, reverse=True),
            "subtitles": subtitles, "automatic_captions": automatic,
            "subtitle_languages": sorted(set(subtitles) | set(automatic)), "language": raw.get("language") or "",
            "chapters": raw.get("chapters") or [], "entries": entries,
            "is_shorts": bool(shorts_signals(evidence)), "is_members_only": is_members_only(raw),
            "needs_analysis": not playlist and not bool(formats),
        }
