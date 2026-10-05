"""Safe named variables: no attribute lookup, expression evaluation or rescraping."""
from __future__ import annotations

from pathlib import Path
import re
from string import Formatter

VARIABLES = frozenset({"title", "original_title", "author", "channel", "artist", "album",
    "album_artist", "track_number", "disc_number", "year", "date", "source", "source_id",
    "collection", "collection_index", "number", "ext", "filename", "media_type"})
NUMERIC = frozenset({"number", "track_number", "disc_number", "collection_index", "year"})


class MetadataVariables:
    @staticmethod
    def from_item(item: dict, number: int, extension: str = "") -> dict:
        data = dict(item.get("metadata") or {})
        data.update({key: value for key, value in item.items() if key != "metadata" and value is not None})
        filename = str(data.get("filename") or data.get("original_filename") or
                       Path(str(data.get("path") or data.get("source_path") or "")).name or
                       data.get("title") or data.get("id") or "untitled")
        stem = Path(filename).stem if Path(filename).suffix else filename
        published = str(data.get("published_at") or data.get("date") or data.get("upload_date") or "")
        if re.fullmatch(r"\d{8}", published):
            published = f"{published[:4]}-{published[4:6]}-{published[6:]}"
        values = {key: data.get(key, "") for key in VARIABLES}
        values.update(filename=stem, ext=extension or Path(filename).suffix.lstrip("."), number=number,
                      title=data.get("title") or stem,
                      original_title=data.get("original_title") or data.get("title") or stem,
                      author=data.get("author") or data.get("uploader") or data.get("artist") or "",
                      channel=data.get("channel") or data.get("author") or data.get("uploader") or "",
                      artist=data.get("artist") or data.get("author") or data.get("uploader") or "",
                      source_id=data.get("source_id") or data.get("id") or "",
                      collection=data.get("collection") or data.get("gallery_name") or data.get("playlist_title") or "",
                      collection_index=data.get("collection_index") or data.get("gallery_index") or data.get("playlist_index") or number,
                      date=published[:10], year=data.get("year") or (published[:4] if published else ""))
        return values


class TemplateEngine:
    @staticmethod
    def render(template: str, variables: dict, padding: int = 0) -> str:
        if not isinstance(template, str) or len(template) > 4096:
            raise ValueError("Шаблон должен быть строкой длиной до 4096 символов.")
        result = []
        try:
            for literal, field, spec, conversion in Formatter().parse(template):
                result.append(literal)
                if field is None:
                    continue
                if field not in VARIABLES or conversion:
                    raise ValueError(f"Неизвестная переменная шаблона: {field}.")
                value = variables.get(field, "")
                if spec:
                    if field not in NUMERIC or not re.fullmatch(r"0?[1-9][0-9]?d?", spec):
                        raise ValueError("Поддерживается только числовая разрядность, например {number:03}.")
                    width = int(spec.rstrip("d"))
                    if width > 12:
                        raise ValueError("Разрядность должна быть не больше 12.")
                    if value not in (None, ""):
                        try:
                            value = f"{int(value):0{width}d}"
                        except (TypeError, ValueError):
                            value = ""  # Missing/non-numeric metadata remains honest.
                elif field == "number" and padding:
                    value = f"{int(value):0{padding}d}"
                result.append(str(value) if value is not None else "")
        except ValueError as error:
            raise ValueError(f"Некорректный шаблон: {error}") from error
        return "".join(result)
