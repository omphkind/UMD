"""Persistent preferences, kept separate from task snapshots."""

from dataclasses import asdict, dataclass, fields
import os
from pathlib import Path
import re
from typing import Any

from app.core.errors import ConfigurationError
from app.storage.atomic import read_json, write_json
from app.storage.lock import FileLock


@dataclass
class Settings:
    yt_dlp_path: str = ""
    deno_path: str = ""
    ffmpeg_path: str = ""
    gallery_dl_path: str = ""
    cookie_file: str = ""
    auth_mode: str = "anonymous"
    output_path: str = ""
    locale: str = "ru-RU"
    language: str = "ru"
    include_shorts: bool = False
    include_members_only: bool = False
    order: str = "oldest_first"
    debug: bool = False
    localization: bool = True
    download_type: str = "video"
    download_quality: str = "best"
    download_container: str = "mp4"
    download_audio: str = "with_audio"
    download_subtitles: str = "none"
    download_thumbnail: bool = False
    default_video_format: str = "mp4"
    default_audio_format: str = "mp3"
    default_photo_format: str = "original"
    default_video_quality: str = "best"
    default_audio_quality: str = "best"
    default_photo_quality: str = "original"
    audio_bitrate: str = "best"
    metadata_preserve: bool = True
    embed_cover: bool = True
    simultaneous_downloads: int = 1
    rename_default_preset: str = "Video"
    rename_before_download: bool = False
    rename_template: str = "{title}"
    rename_number_start: int = 1
    rename_number_step: int = 1
    rename_number_padding: int = 0
    collision_policy: str = "ask"
    folder_organization: bool = False
    folder_template: str = "{source}/{collection}"
    gallery_page_size: int = 80
    thumbnail_cache: bool = True
    lazy_loading: bool = True
    gallery_max_concurrency: int = 3

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        if not isinstance(data, dict):
            raise ConfigurationError("Настройки должны быть JSON-объектом.")
        # Beta 2 stored one active media preference. Keep it when introducing
        # per-type defaults, while explicit newer preferences always win.
        data = dict(data)
        kind = data.get("download_type", "video")
        formats = {"video": {"mp4", "mkv", "webm", "mov", "original"},
                   "audio": {"mp3", "m4a", "opus", "wav", "flac", "aac", "original"},
                   "photo": {"original", "jpg", "jpeg", "png", "webp", "avif", "gif"}}
        if isinstance(kind, str) and kind in formats:
            container = data.get("download_container")
            if isinstance(container, str) and container in formats[kind]:
                data.setdefault(f"default_{kind}_format", container)
            if "download_quality" in data:
                data.setdefault(f"default_{kind}_quality", data["download_quality"])
        allowed = {f.name for f in fields(cls)}
        if set(data) - allowed:
            raise ConfigurationError("Настройки содержат неизвестные поля.")
        result = cls(**data)
        for name in ("include_shorts", "include_members_only", "debug", "localization", "download_thumbnail",
                     "metadata_preserve", "embed_cover", "rename_before_download", "folder_organization",
                     "thumbnail_cache", "lazy_loading"):
            if not isinstance(getattr(result, name), bool):
                raise ConfigurationError(f"Настройка {name} должна быть логическим значением.")
        for name in ("yt_dlp_path", "deno_path", "ffmpeg_path", "output_path", "locale", "language", "order", "download_type", "download_quality", "download_container", "download_audio", "download_subtitles",
                     "gallery_dl_path", "cookie_file", "auth_mode", "default_video_format", "default_audio_format",
                     "default_photo_format", "default_video_quality", "default_audio_quality", "default_photo_quality",
                     "audio_bitrate", "rename_default_preset", "rename_template", "collision_policy", "folder_template"):
            if not isinstance(getattr(result, name), str):
                raise ConfigurationError(f"Настройка {name} должна быть строкой.")
        if result.order not in {"oldest_first", "newest_first"}:
            raise ConfigurationError("Порядок должен быть oldest_first или newest_first.")
        if not result.locale or not result.language:
            raise ConfigurationError("Язык и локаль не могут быть пустыми.")
        if result.download_type not in {"video", "audio", "photo", "subtitles", "thumbnail", "metadata"}:
            raise ConfigurationError("Неизвестный тип загрузки.")
        if result.download_container not in {"mp4", "mkv", "webm", "mov", "original", "mp3", "m4a", "opus", "wav", "flac", "aac", "jpg", "jpeg", "png", "webp", "avif", "gif"}:
            raise ConfigurationError("Неизвестный контейнер загрузки.")
        if result.download_audio not in {"with_audio", "video_only", "audio_only"}:
            raise ConfigurationError("Неизвестный режим аудио.")
        quality = result.download_quality
        if not re.fullmatch(r"best|original|large|medium|small|[1-9][0-9]{1,4}p?|format:[A-Za-z0-9_.:-]+", quality):
            raise ConfigurationError("Качество: best, высота видео или format:ID.")
        if not re.fullmatch(r"[\w.-]+", result.download_subtitles):
            raise ConfigurationError("Некорректный язык субтитров.")
        if result.auth_mode not in {"anonymous", "cookies"}:
            raise ConfigurationError("Режим авторизации: anonymous или cookies.")
        if any(char in result.cookie_file for char in "\r\n\x00"):
            raise ConfigurationError("Укажите путь к файлу cookies, а не содержимое сессии.")
        if result.collision_policy not in {"ask", "skip", "overwrite", "append_number"}:
            raise ConfigurationError("Неизвестная политика совпадения имён.")
        for name, low, high in (("simultaneous_downloads", 1, 8), ("gallery_max_concurrency", 1, 8),
                                ("gallery_page_size", 1, 200), ("rename_number_start", 0, 1000000000),
                                ("rename_number_step", 1, 1000000), ("rename_number_padding", 0, 12)):
            value = getattr(result, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ConfigurationError(f"Настройка {name}: целое число от {low} до {high}.")
        if result.default_video_format not in {"mp4", "mkv", "webm", "mov", "original"}:
            raise ConfigurationError("Неизвестный формат видео по умолчанию.")
        if result.default_audio_format not in {"mp3", "m4a", "opus", "wav", "flac", "aac", "original"}:
            raise ConfigurationError("Неизвестный формат аудио по умолчанию.")
        if result.default_photo_format not in {"original", "jpg", "jpeg", "png", "webp", "avif", "gif"}:
            raise ConfigurationError("Неизвестный формат фото по умолчанию.")
        for name in ("default_video_quality", "default_audio_quality", "default_photo_quality"):
            if not re.fullmatch(r"best|original|large|medium|small|[1-9][0-9]{1,4}p?|format:[A-Za-z0-9_.:-]+", getattr(result, name)):
                raise ConfigurationError(f"Некорректная настройка {name}.")
        if not re.fullmatch(r"best|[1-9][0-9]{1,3}k?", result.audio_bitrate):
            raise ConfigurationError("Битрейт аудио: best или число кбит/с.")
        return result


def default_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "UMD" if local else Path.home() / ".local" / "share" / "UMD"


class SettingsStore:
    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
        self.path = self.data_dir / "config.json"

    def load(self) -> Settings:
        with FileLock(self.path.with_suffix(".lock")):
            if not self.path.exists():
                return Settings(output_path=str(self.data_dir / "output"))
            return Settings.from_dict(read_json(self.path))

    def save(self, settings: Settings) -> None:
        with FileLock(self.path.with_suffix(".lock")):
            validated = Settings.from_dict(settings.to_dict())
            if self.path.exists():
                Settings.from_dict(read_json(self.path))
            write_json(self.path, validated.to_dict(), backup=True)
