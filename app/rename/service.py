"""Offline rename plans and transactional local-file application.

Preview never modifies files. Apply stages every source before moving any
destination, allowing swaps/cycles and rollback without losing overwritten files.
"""
from __future__ import annotations

from copy import deepcopy
import errno
import os
from pathlib import Path
import re
import shutil
import threading
import uuid

from app.rename.collisions import CollisionResolver, path_key
from app.rename.numbering import NumberingEngine
from app.rename.rules import RuleEngine
from app.rename.sanitizer import FilenameSanitizer, utf16_length
from app.rename.templates import MetadataVariables, TemplateEngine


class RenameError(ValueError):
    """A safe, user-facing planning or application error."""


_APPLY_LOCK = threading.Lock()


def _install(source: Path, target: Path):
    """Move to a vacant name without a check/replace race."""
    if os.name == "nt":
        try:
            os.rename(source, target)  # Windows rename refuses existing targets.
            return
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
    else:
        try:
            os.link(source, target)
            source.unlink()
            return
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
    created = False
    try:
        with source.open("rb") as src, target.open("xb") as dst:
            created = True
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        shutil.copystat(source, target)
        source.unlink()
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise


def _move(source: Path, target: Path):
    """Atomic same-volume move, guarded copy/delete across volumes."""
    try:
        os.replace(source, target)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        # Exclusive copy prevents racing another writer on the destination.
        created = False
        try:
            with source.open("rb") as src, target.open("xb") as dst:
                created = True
                shutil.copyfileobj(src, dst, 1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            shutil.copystat(source, target)
            source.unlink()
        except BaseException:
            if created:
                target.unlink(missing_ok=True)
            raise


def _ensure_inside(path: Path, root: Path):
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise RenameError("Путь выходит за пределы папки вывода.") from error
    # A symlink/junction to an existing location must not hide unexpected files.
    cursor = path.parent
    while cursor != root and cursor != cursor.parent:
        if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
            raise RenameError("Папка назначения содержит символическую ссылку.")
        cursor = cursor.parent


def _folder(template: str, values: dict, padding: int) -> Path:
    template = template.replace("\\", "/")
    if template.startswith("/") or re.match(r"^[A-Za-z]:", template):
        raise RenameError("Шаблон папки должен быть относительным.")
    parts = template.split("/")
    if "{filename}" in parts:
        if parts[-1] != "{filename}" or parts.count("{filename}") != 1:
            raise RenameError("{filename} допускается только в конце шаблона папки.")
        parts.pop()
    result = []
    for part in parts:
        if part in {".", ".."}:
            raise RenameError("Переходы . и .. в шаблоне папки запрещены.")
        if not part:
            continue
        rendered = TemplateEngine.render(part, values, padding).strip()
        if rendered in {".", ".."}:
            raise RenameError("Шаблон создал недопустимый переход папки.")
        if rendered:
            result.append(FilenameSanitizer.sanitize(rendered, 120))
    return Path(*result)


class RenameService:
    def preview(self, items: list[dict], options: dict, output_dir: str | Path,
                local_files: bool = False) -> list[dict]:
        """Return a complete plan without downloading, scraping or mutating files."""
        if not isinstance(options, dict):
            raise RenameError("Настройки переименования должны быть JSON-объектом.")
        options = deepcopy(options.get("rename", options))
        root = Path(output_dir).expanduser().resolve()
        numbering = NumberingEngine(int(options.get("start", 1)), int(options.get("step", 1)),
                                    int(options.get("padding", 0)))
        sources = [item.get("path") or item.get("source_path") or item.get("local_path") for item in items] if local_files else []
        collisions = CollisionResolver(sources, options.get("reserved_paths", []))
        policy = options.get("collision_policy", "ask")
        if policy not in CollisionResolver.POLICIES:
            raise RenameError("Неизвестная политика конфликтов.")
        plan = []
        seen_sources = set()
        for index, item in enumerate(items):
            number = numbering.value(index)
            source = sources[index] if local_files else None
            row = {"source_path": str(Path(source).resolve()) if source else None,
                   "target_path": "", "filename": "", "number": number, "status": "ready",
                   "conflict": "", "skip": False, "collision_policy": policy,
                   "metadata": deepcopy(item.get("metadata") or item), "output_dir": str(root),
                   "original_filename": str((Path(source).name if local_files and source else "") or
                                             item.get("filename") or item.get("original_filename") or
                                             item.get("title") or item.get("id") or "untitled")}
            try:
                if local_files:
                    if not source or not Path(source).is_file() or Path(source).is_symlink():
                        raise RenameError("Исходный файл не найден или является символической ссылкой.")
                    source_key = path_key(source)
                    if source_key in seen_sources:
                        raise RenameError("Исходный файл повторяется в пакете.")
                    seen_sources.add(source_key)
                naming_item = dict(item)
                if local_files and source:
                    # One completed task can include media, subtitles and JSON
                    # sidecars. Source metadata describes the media, whereas the
                    # local filesystem determines each selected file's extension.
                    original_name = Path(source).name
                    extension = Path(source).suffix.lstrip(".")
                    naming_item.update(filename=original_name, original_filename=original_name, ext=extension)
                else:
                    original_name = str(item.get("filename") or item.get("original_filename") or "")
                    extension = str(item.get("ext") or item.get("extension") or Path(original_name).suffix.lstrip(".") or "")
                values = MetadataVariables.from_item(naming_item, number, extension)
                template = options.get("template", "{title}") if options.get("enabled", True) else "{filename}"
                stem = TemplateEngine.render(template, values, numbering.padding)
                # {ext} may be explicitly written in the filename template.
                if extension and stem.casefold().endswith("." + extension.casefold()):
                    stem = stem[:-(len(extension) + 1)]
                stem = RuleEngine.apply(stem, options.get("rules", []))
                destination = root
                if options.get("organization_enabled", False):
                    destination /= _folder(str(options.get("folder_template", "")), values, numbering.padding)
                _ensure_inside(destination / "placeholder", root)
                limit = min(180, 240 - utf16_length(str(destination)) - 1)
                filename = FilenameSanitizer.filename(stem, extension, limit)
                target, status, conflict, existing = collisions.resolve(destination / filename, policy)
                _ensure_inside(target, root)
                if utf16_length(str(target)) > 240:
                    raise RenameError("Слишком длинный путь: выберите более короткую папку вывода.")
                if existing and existing.is_dir():
                    status, conflict = "conflict", "Папка уже использует это имя."
                # Case-only changes are intentional on Windows; exact names are unchanged.
                if status == "ready" and source and str(Path(source).absolute()) == str(target.absolute()):
                    status = "unchanged"
                row.update(target_path=str(target), filename=target.name, status=status,
                           conflict=conflict, skip=status == "skipped",
                           existing_path=str(existing) if existing else None)
            except (ValueError, OSError, TypeError) as error:
                row.update(status="error", conflict=str(error))
            plan.append(row)
        return plan

    def apply(self, plan: list[dict]) -> dict[str, str]:
        """Apply an explicitly accepted local-file plan; return changed path mapping.

        Any failure rolls back completed targets, staged sources and overwrite
        backups. Backups survive a rollback error and their paths are reported.
        """
        with _APPLY_LOCK:
            return self._apply(plan)

    def _apply(self, plan: list[dict]) -> dict[str, str]:
        if any(row.get("status") in {"error", "conflict"} for row in plan):
            raise RenameError("В плане есть конфликты. Исправьте их перед применением.")
        rows = [row for row in plan if row.get("status") == "ready" and not row.get("skip")]
        sources, targets = set(), set()
        for row in rows:
            if not row.get("source_path") or not row.get("target_path"):
                raise RenameError("Для применения нужны локальные исходные файлы и пути назначения.")
            source, target = Path(row["source_path"]), Path(row["target_path"])
            if not source.is_file() or source.is_symlink():
                raise RenameError(f"Исходный файл недоступен: {source.name}.")
            if path_key(source) in sources or path_key(target) in targets:
                raise RenameError("План содержит повторяющиеся исходные или целевые пути.")
            sources.add(path_key(source))
            targets.add(path_key(target))
            if target.name != FilenameSanitizer.sanitize(target.name, 255) or utf16_length(str(target)) > 240:
                raise RenameError("Недопустимое имя или слишком длинный путь назначения.")
            _ensure_inside(target, Path(row.get("output_dir", target.parent)))
        # Fresh collision checks catch files created after the preview.
        checker = CollisionResolver([row["source_path"] for row in rows])
        for row in rows:
            target = Path(row["target_path"])
            existing = checker.existing(target)
            if existing and path_key(existing) not in sources:
                if row.get("collision_policy") != "overwrite" or not existing.is_file() or existing.is_symlink():
                    raise RenameError(f"После предпросмотра появился конфликт: {target.name}.")
            if target.parent.exists() and not target.parent.is_dir():
                raise RenameError("Путь назначения занят файлом.")
        staged = []
        backups = []
        installed = []
        created_dirs = []
        token = uuid.uuid4().hex
        try:
            # Phase one frees every original name, including swap/cycle targets.
            for index, row in enumerate(rows):
                source = Path(row["source_path"])
                stage = source.with_name(f".umd-rename-{token}-{index}.tmp")
                _install(source, stage)
                staged.append((source, stage, Path(row["target_path"])))
            for index, (source, stage, target) in enumerate(staged):
                missing = []
                parent = target.parent
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for directory in reversed(missing):
                    directory.mkdir()
                    created_dirs.append(directory)
                existing = CollisionResolver().existing(target)
                if existing:
                    if rows[index].get("collision_policy") != "overwrite" or not existing.is_file() or existing.is_symlink():
                        raise RenameError(f"Путь уже занят: {target.name}.")
                    backup = existing.with_name(f".umd-backup-{token}-{index}.tmp")
                    _install(existing, backup)
                    backups.append((existing, backup))
                # Targets outside this process can still appear; do not silently replace them.
                if target.exists():
                    raise RenameError(f"Путь уже занят: {target.name}.")
                _install(stage, target)
                installed.append((source, stage, target))
        except BaseException as error:
            rollback_errors = []
            # Put all installed files back into unique staging names before sources.
            for source, stage, target in reversed(installed):
                try:
                    _move(target, stage)
                except OSError as rollback_error:
                    rollback_errors.append(f"{target}: {rollback_error}")
            for existing, backup in reversed(backups):
                try:
                    _move(backup, existing)
                except OSError as rollback_error:
                    rollback_errors.append(f"{backup}: {rollback_error}")
            for source, stage, target in reversed(staged):
                if stage.exists():
                    try:
                        _move(stage, source)
                    except OSError as rollback_error:
                        rollback_errors.append(f"{stage}: {rollback_error}")
            for directory in reversed(created_dirs):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            detail = " Изменения отменены." if not rollback_errors else " Не удалось восстановить: " + "; ".join(rollback_errors)
            raise RenameError(f"Переименование не выполнено: {error}.{detail}") from error
        for existing, backup in backups:
            try:
                backup.unlink()
            except OSError:
                # A leftover backup is safer than turning a completed transaction into failure.
                pass
        return {str(source): str(target) for source, stage, target in installed}
