"""Replace each export atomically; include only successfully processed objects."""

from pathlib import Path
from typing import Protocol

from app.core.errors import StorageError
from app.core.models import MediaItem, Progress
from app.storage.atomic import atomic_write, write_json
from app.storage.lock import FileLock


class ExportProvider(Protocol):
    def export(self, progress: Progress, output_dir: Path) -> Path: ...


def successful_items(progress: Progress) -> list[MediaItem]:
    return sorted((item for item in progress.items.values() if item.status == "success"), key=lambda item: item.number)


def one_line(value: str) -> str:
    return " ".join(str(value or "").split())


class UrlExporter:
    def export(self, progress: Progress, output_dir: str | Path) -> Path:
        path = Path(output_dir) / "UMD_URL.txt"
        urls = [item.url for item in successful_items(progress)]
        if any("\n" in url or "\r" in url or not url.startswith(("https://", "http://")) for url in urls):
            raise StorageError("Некорректный URL в задаче; экспорт отменён.")
        atomic_write(path, ("\n".join(urls) + ("\n" if urls else "")).encode("utf-8"))
        return path


class MetaFinExporter:
    def export(self, progress: Progress, output_dir: str | Path) -> Path:
        path = Path(output_dir) / "UMD_META.txt"
        blocks = [f"[{item.number:03d}]\ntitle={one_line(item.title)}\noriginal title={one_line(item.original_title)}\noverview={one_line(item.overview)}" for item in successful_items(progress)]
        atomic_write(path, ("\n\n".join(blocks) + ("\n" if blocks else "")).encode("utf-8"))
        return path


class JsonExporter:
    def export(self, progress: Progress, output_dir: str | Path) -> Path:
        path = Path(output_dir) / "UMD_META.json"
        data = progress.to_dict()
        data["items"] = {item.key: item.to_dict() for item in successful_items(progress)}
        write_json(path, data)
        return path


def export_all(progress: Progress, output_dir: str | Path) -> dict[str, Path]:
    output_dir = Path(output_dir)
    with FileLock(output_dir / ".umd-export.lock"):
        exporters = {"urls": UrlExporter(), "metafin": MetaFinExporter(), "json": JsonExporter()}
        return {name: exporter.export(progress, output_dir) for name, exporter in exporters.items()}
