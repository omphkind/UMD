"""Versioned progress snapshots with strict corruption handling."""

from pathlib import Path

from app.core.errors import StorageError
from app.core.models import Progress
from app.storage.atomic import read_json, write_json
from app.storage.lock import FileLock


class ProgressStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def lock(self) -> FileLock:
        return FileLock(Path(str(self.path) + ".lock"))

    def load(self) -> Progress | None:
        with self.lock():
            if not self.path.exists():
                return None
            try:
                return Progress.from_dict(read_json(self.path))
            except (ValueError, TypeError, KeyError) as exc:
                raise StorageError(f"Некорректное состояние {self.path}. Оно сохранено; проверьте {self.path}.bak.") from exc

    def save(self, progress: Progress) -> None:
        with self.lock():
            try:
                Progress.from_dict(progress.to_dict())
            except (ValueError, TypeError, KeyError) as exc:
                raise StorageError("Некорректное состояние задачи; запись отменена.") from exc
            if self.path.exists():
                self.load()  # Never overwrite a malformed existing snapshot.
            write_json(self.path, progress.to_dict(), backup=True)
