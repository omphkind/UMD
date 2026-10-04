"""Persistent preferences, kept separate from task snapshots."""

from dataclasses import asdict, dataclass, fields
import os
from pathlib import Path
from typing import Any

from app.core.errors import ConfigurationError
from app.storage.atomic import read_json, write_json
from app.storage.lock import FileLock


@dataclass
class Settings:
    yt_dlp_path: str = ""
    deno_path: str = ""
    output_path: str = ""
    locale: str = "ru-RU"
    language: str = "ru"
    include_shorts: bool = False
    include_members_only: bool = False
    order: str = "oldest_first"
    debug: bool = False
    localization: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        if not isinstance(data, dict):
            raise ConfigurationError("Настройки должны быть JSON-объектом.")
        allowed = {f.name for f in fields(cls)}
        if set(data) - allowed:
            raise ConfigurationError("Настройки содержат неизвестные поля.")
        result = cls(**data)
        for name in ("include_shorts", "include_members_only", "debug", "localization"):
            if not isinstance(getattr(result, name), bool):
                raise ConfigurationError(f"Настройка {name} должна быть логическим значением.")
        for name in ("yt_dlp_path", "deno_path", "output_path", "locale", "language", "order"):
            if not isinstance(getattr(result, name), str):
                raise ConfigurationError(f"Настройка {name} должна быть строкой.")
        if result.order not in {"oldest_first", "newest_first"}:
            raise ConfigurationError("Порядок должен быть oldest_first или newest_first.")
        if not result.locale or not result.language:
            raise ConfigurationError("Язык и локаль не могут быть пустыми.")
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
