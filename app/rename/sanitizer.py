"""Windows names without stripping meaningful Unicode text."""
from __future__ import annotations

import re
import unicodedata

INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def truncate(value: str, limit: int) -> str:
    while value and utf16_length(value) > limit:
        value = value[:-1]
    return value


class FilenameSanitizer:
    @staticmethod
    def sanitize(value: object, max_length: int = 180) -> str:
        name = unicodedata.normalize("NFC", str(value or ""))
        name = INVALID.sub("_", name).strip().rstrip(". ")
        if not name or name in {".", ".."}:
            name = "untitled"
        if RESERVED.match(name):
            name = "_" + name
        name = truncate(name, max_length).rstrip(". ")
        return name or "untitled"

    @staticmethod
    def filename(stem: str, extension: str, max_length: int = 180) -> str:
        ext = FilenameSanitizer.sanitize(extension.lstrip("."), 20) if extension else ""
        suffix = "." + ext if ext else ""
        if max_length <= utf16_length(suffix) + 1:
            raise ValueError("Слишком длинный путь: выберите более короткую папку вывода.")
        return FilenameSanitizer.sanitize(stem, max_length - utf16_length(suffix)) + suffix
