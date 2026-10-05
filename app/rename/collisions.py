"""Windows case-insensitive collision checks, also deterministic on other hosts."""
from pathlib import Path


def path_key(path: Path | str) -> str:
    return str(Path(path).resolve()).casefold()


class CollisionResolver:
    POLICIES = {"ask", "skip", "overwrite", "append_number"}

    def __init__(self, source_paths=(), reserved_paths=()):
        self.sources = {path_key(path) for path in source_paths if path}
        self.reserved = {path_key(path) for path in reserved_paths if path}
        self.directories = {}

    def existing(self, path: Path) -> Path | None:
        directory = path.parent
        key = path_key(directory)
        if key not in self.directories:
            self.directories[key] = {entry.name.casefold(): entry for entry in directory.iterdir()} if directory.is_dir() else {}
        return self.directories[key].get(path.name.casefold())

    def resolve(self, path: Path, policy: str) -> tuple[Path, str, str, Path | None]:
        if policy not in self.POLICIES:
            raise ValueError("Неизвестная политика конфликтов.")
        key = path_key(path)
        existing = self.existing(path)
        batch = key in self.reserved
        occupied = existing is not None and path_key(existing) not in self.sources
        if not batch and not occupied:
            self.reserved.add(key)
            return path, "ready", "", existing
        reason = "Повторяющееся имя в пакете." if batch else "Файл уже существует."
        if policy == "append_number":
            original = path
            counter = 1
            while key in self.reserved or (self.existing(path) is not None and key not in self.sources):
                path = original.with_name(f"{original.stem} ({counter}){original.suffix}")
                key = path_key(path)
                counter += 1
            self.reserved.add(key)
            return path, "ready", reason, None
        if policy == "skip":
            return path, "skipped", reason, existing
        if policy == "overwrite" and not batch and existing and existing.is_file():
            self.reserved.add(key)
            return path, "ready", reason, existing
        return path, "conflict", reason, existing
