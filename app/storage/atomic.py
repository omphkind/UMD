"""Replace files only after their complete contents reach stable storage."""

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from app.core.errors import StorageError


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageError(f"Не удалось прочитать {path}. Файл сохранён; проверьте резервную копию {path}.bak.") from exc


def atomic_write(path: Path, content: bytes) -> None:
    path = Path(path)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=path.name + ".", suffix=".tmp", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise StorageError(f"Не удалось сохранить {path}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_json(path: Path, data: Any, *, backup: bool = False) -> None:
    path = Path(path)
    # Serialize before changing any existing files.
    try:
        content = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise StorageError("Состояние содержит данные, которые нельзя записать в JSON.") from exc
    if backup and path.exists():
        read_json(path)  # Corruption is an error, never an implicit reset.
        try:
            previous = path.read_bytes()
        except OSError as exc:
            raise StorageError(f"Не удалось сохранить резервную копию {path}.") from exc
        atomic_write(Path(str(path) + ".bak"), previous)
    atomic_write(path, content)
