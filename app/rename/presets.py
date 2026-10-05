"""Versioned editable JSON presets; defaults are data, not naming logic."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json

from app.storage.atomic import write_json

DEFAULT_FILE = Path(__file__).with_name("default_presets.json")


class PresetStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else None

    def load(self) -> dict:
        path = self.path if self.path and self.path.exists() else DEFAULT_FILE
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("presets"), dict):
            raise ValueError("Неподдерживаемый формат пресетов переименования.")
        if any(not isinstance(name, str) or not isinstance(options, dict)
               for name, options in data["presets"].items()):
            raise ValueError("Пресеты должны содержать имена и JSON-объекты настроек.")
        for name, options in data["presets"].items():
            for field in ("template", "folder_template", "preset"):
                if field in options and not isinstance(options[field], str):
                    raise ValueError(f"Пресет {name}: поле {field} должно быть строкой.")
            if "rules" in options and (not isinstance(options["rules"], list) or
                                       any(not isinstance(rule, dict) for rule in options["rules"])):
                raise ValueError(f"Пресет {name}: правила должны быть списком объектов.")
            for field in ("enabled", "organization_enabled"):
                if field in options and not isinstance(options[field], bool):
                    raise ValueError(f"Пресет {name}: поле {field} должно быть логическим значением.")
            for field in ("start", "step", "padding"):
                if field in options and (not isinstance(options[field], int) or isinstance(options[field], bool)):
                    raise ValueError(f"Пресет {name}: поле {field} должно быть целым числом.")
        return deepcopy(data)

    def save(self, data: dict):
        if not self.path:
            raise ValueError("Не выбрана папка хранения пресетов.")
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("presets"), dict):
            raise ValueError("Некорректный формат пресетов переименования.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.path, data)

    def set(self, name: str, options: dict):
        if not name.strip():
            raise ValueError("Введите имя пресета.")
        data = self.load()
        data["presets"][name.strip()] = deepcopy(options)
        self.save(data)

    def delete(self, name: str):
        data = self.load()
        data["presets"].pop(name, None)
        self.save(data)

    def list(self) -> dict[str, dict]:
        return self.load()["presets"]

    def get(self, name: str) -> dict:
        return self.list().get(name, {})

    upsert = set
