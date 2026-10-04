"""YouTube URL validation, discovery and evidence-based content classification."""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import parse_qs, unquote, urlparse

from app.core.errors import MediaError
from app.downloader.ytdlp import YtDlp

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
PLAYLIST_ID = re.compile(r"[A-Za-z0-9_-]{10,}\Z")
HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}


@dataclass(frozen=True)
class YouTubeResource:
    kind: str
    identifier: str
    url: str
    is_shorts: bool = False


def video_url(media_id: str) -> str:
    if not VIDEO_ID.fullmatch(media_id):
        raise MediaError("unsupported_url", "Некорректный YouTube video ID.")
    return f"https://www.youtube.com/watch?v={media_id}"


def parse_youtube_url(url: str) -> YouTubeResource:
    if not isinstance(url, str):
        raise MediaError("unsupported_url", "Введите URL YouTube.")
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in HOSTS or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            raise ValueError
        path = unquote(parsed.path).strip("/")
        parts = path.split("/") if path else []
        query = parse_qs(parsed.query)
        candidate = None
        shorts = False
        if parsed.hostname in {"youtu.be", "www.youtu.be"}:
            candidate = parts[0] if len(parts) == 1 else None
        elif path == "watch":
            candidate = query.get("v", [None])[0]
        elif len(parts) == 2 and parts[0] in {"shorts", "embed", "live"}:
            candidate = parts[1]
            shorts = parts[0] == "shorts"
        if candidate and VIDEO_ID.fullmatch(candidate):
            return YouTubeResource("video", candidate, video_url(candidate), shorts)
        if path == "playlist" and PLAYLIST_ID.fullmatch(query.get("list", [""])[0]):
            playlist = query["list"][0]
            return YouTubeResource("playlist", playlist, f"https://www.youtube.com/playlist?list={playlist}")
        tab = ""
        if parts and parts[-1] in {"videos", "shorts", "streams"}:
            tab = "/" + parts.pop()
        if len(parts) == 1 and re.fullmatch(r"@[\w.\-]{1,100}", parts[0], re.UNICODE):
            channel = parts[0]
        elif len(parts) == 2 and parts[0] in {"channel", "c", "user"} and re.fullmatch(r"[\w.\-]{1,100}", parts[1], re.UNICODE):
            channel = "/".join(parts)
            if parts[0] == "channel" and not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", parts[1]):
                raise ValueError
        else:
            raise ValueError
        return YouTubeResource("channel", channel, f"https://www.youtube.com/{channel}{tab}")
    except (ValueError, TypeError, IndexError):
        raise MediaError("unsupported_url", "Нужен корректный URL видео, Shorts, плейлиста или канала YouTube.") from None


def shorts_signals(data: dict) -> list[str]:
    signals = []
    if data.get("is_shorts") is True or data.get("is_short") is True:
        signals.append("explicit_flag")
    if data.get("media_type") in {"short", "shorts"}:
        signals.append("media_type")
    if data.get("source_tab") == "shorts":
        signals.append("channel_shorts_tab")
    if any("/shorts/" in str(data.get(key, "")) for key in ("url", "webpage_url", "original_url")):
        signals.append("shorts_url")
    # Duration alone cannot distinguish regular short videos from Shorts.
    # Explicit extractor/category evidence is preferred to guesses from thumbnails.
    return signals


def is_members_only(data: dict) -> bool:
    if data.get("is_members_only") is True or data.get("availability") in {"subscriber_only", "premium_only"}:
        return True
    text = " ".join(str(data.get(key) or "") for key in ("availability", "error", "message", "availability_message", "badges"))
    text = text.lower()
    return any(hint in text for hint in ("members-only", "member-only", "members only", "channel's members", "join this channel", "только для спонсоров"))


class YouTubeSource:
    name = "youtube"

    def __init__(self, settings, downloader=None):
        self.settings = settings
        self.downloader = downloader or YtDlp(settings)

    def accepts(self, url: str) -> bool:
        try:
            parse_youtube_url(url)
            return True
        except MediaError:
            return False

    def fetch(self, url: str) -> dict:
        resource = parse_youtube_url(url)
        if resource.kind != "video":
            raise MediaError("unsupported_url", "Получение метаданных требует URL отдельного видео.")
        return self.downloader.extract(resource.url)

    def discover(self, url: str) -> list[dict]:
        resource = parse_youtube_url(url)
        if resource.kind == "video":
            return [{"media_id": resource.identifier, "url": resource.url, "title": "",
                     "is_shorts": resource.is_shorts, "is_members_only": False,
                     "published_at": None, "metadata": {"original_url": url, "is_shorts": resource.is_shorts}}]
        raw = self.downloader.extract(resource.url, flat=True)
        found = {}

        def visit(data: dict, inherited_tab: str = ""):
            if not isinstance(data, dict):
                return
            tab = inherited_tab
            container_url = str(data.get("webpage_url") or data.get("url") or "")
            if container_url.rstrip("/").endswith("/shorts"):
                tab = "shorts"
            entries = data.get("entries")
            if entries is not None:
                for entry in entries:
                    visit(entry, tab)
                return
            media_id = str(data.get("id") or "")
            if not VIDEO_ID.fullmatch(media_id):
                try:
                    media_id = parse_youtube_url(str(data.get("url") or data.get("webpage_url") or "")).identifier
                except MediaError:
                    return
                if not VIDEO_ID.fullmatch(media_id):
                    return
            metadata = dict(data)
            if tab:
                metadata["source_tab"] = tab
            entry = {"media_id": media_id, "url": video_url(media_id), "title": data.get("title") or "",
                     "is_shorts": bool(shorts_signals(metadata)), "is_members_only": is_members_only(metadata),
                     "published_at": data.get("upload_date") or data.get("release_date"), "metadata": metadata}
            if media_id in found:
                found[media_id]["is_shorts"] |= entry["is_shorts"]
                found[media_id]["is_members_only"] |= entry["is_members_only"]
            else:
                found[media_id] = entry

        visit(raw, "shorts" if resource.url.endswith("/shorts") else "")
        return list(found.values())
