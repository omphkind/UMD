"""Interactive Windows console menu and scriptable commands."""

import argparse
from dataclasses import replace
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys

from app import __version__
from app.core.config import Settings, SettingsStore
from app.core.errors import UmdError
from app.export import export_all
from app.storage.progress import ProgressStore
from app.ui.application import run_task
from app.ui.diagnostics import environment_check, self_test


def parser():
    result = argparse.ArgumentParser(prog="UMD", description="UMD — Universal Media Downloader")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--data-dir", type=Path, help="Отдельный каталог задачи, настроек и логов")
    result.add_argument("--check-environment", action="store_true")
    result.add_argument("--self-test", action="store_true")
    commands = result.add_subparsers(dest="command")
    process = commands.add_parser("process", help="Новая обработка YouTube URL")
    process.add_argument("urls", nargs="+")
    process.add_argument("--include-shorts", action=argparse.BooleanOptionalAction, default=None)
    process.add_argument("--include-members-only", action=argparse.BooleanOptionalAction, default=None)
    process.add_argument("--localization", action=argparse.BooleanOptionalAction, default=None)
    process.add_argument("--order", choices=["oldest_first", "newest_first"])
    for command in ("resume", "update", "retry", "history", "export"):
        commands.add_parser(command)
    settings = commands.add_parser("settings", help="Посмотреть/изменить сохранённые настройки")
    settings.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    return result


def setup_logging(data_dir, settings):
    directory = data_dir / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / "umd.log", maxBytes=2_000_000,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO,
                        handlers=[handler], force=True)


def set_preferences(store, settings, pairs):
    values = settings.to_dict()
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or key not in values:
            raise ValueError("Настройка должна иметь вид имя=значение; список доступен через settings.")
        if isinstance(values[key], bool):
            choices = {"true": True, "false": False, "on": True, "off": False, "1": True, "0": False}
            if value.lower() not in choices:
                raise ValueError(f"{key}: укажите true или false")
            values[key] = choices[value.lower()]
        else:
            values[key] = value
    updated = Settings.from_dict(values)
    store.save(updated)
    return updated


def task_screen(settings):
    print("\nНовая обработка | YouTube | режим: метаданные")
    print("Введите URL видео, канала или плейлиста. Несколько URL — по одному в строке; пустая строка завершает список.")
    urls = []
    while True:
        value = input("URL: ").strip()
        if not value:
            break
        urls.append(value)
    if not urls:
        return [], settings
    values = {}
    for key, label in [("include_shorts", "Включать Shorts"),
                       ("include_members_only", "Включать Members-only"),
                       ("localization", "Использовать локализацию YouTube")]:
        default = getattr(settings, key)
        answer = input(f"{label} [{'ON' if default else 'OFF'}], Enter — сохранить, y/n: ").strip().lower()
        if answer:
            if answer not in {"y", "n", "д", "н"}:
                raise ValueError("Введите y или n")
            values[key] = answer in {"y", "д"}
    order = input(f"Порядок [{settings.order}]: 1 — от старых, 2 — от новых, Enter — сохранить: ").strip()
    if order:
        if order not in {"1", "2"}:
            raise ValueError("Порядок: 1 или 2")
        values["order"] = "oldest_first" if order == "1" else "newest_first"
    return urls, replace(settings, **values)


def saved_command(command, data_dir):
    progress = ProgressStore(data_dir / "UMD_PROGRESS.json").load()
    if progress is None:
        print("Предыдущая задача не найдена.")
        return
    if command == "history":
        print(json.dumps(progress.history, ensure_ascii=False, indent=2))
    else:
        settings = Settings.from_dict(progress.settings)
        paths = export_all(progress, Path(settings.output_path or data_dir / "output").expanduser())
        print("Экспорт: " + ", ".join(str(path) for path in paths.values()))


def menu(store, settings):
    print(f"UMD — Universal Media Downloader | {__version__}")
    print(f"Данные: {store.data_dir}")
    environment_check(settings, store.data_dir)
    while True:
        print("\n1. Новая обработка\n2. Продолжить предыдущую\n3. Обновить источник\n4. Повторить только ошибки\n5. Настройки\n0. Выход")
        try:
            choice = input("> ").strip()
            if choice == "0":
                return 0
            if choice == "1":
                urls, selected = task_screen(settings)
                if urls:
                    run_task(store.data_dir, selected, "new", urls)
            elif choice in {"2", "3", "4"}:
                run_task(store.data_dir, settings, {"2": "resume", "3": "update", "4": "retry"}[choice])
            elif choice == "5":
                print(json.dumps(settings.to_dict(), ensure_ascii=False, indent=2))
                pair = input("Изменение имя=значение, Enter — назад: ").strip()
                if pair:
                    settings = set_preferences(store, settings, [pair])
                    setup_logging(store.data_dir, settings)
                    print("Настройки сохранены.")
            else:
                print("Выберите пункт 0–5.")
        except KeyboardInterrupt:
            print("\nОбработка прервана. Прогресс сохранён; выберите «Продолжить предыдущую».")
        except (UmdError, ValueError, OSError) as error:
            print(f"Ошибка: {error}")
            logging.getLogger(__name__).debug("Operation failed", exc_info=settings.debug)
        except EOFError:
            return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    try:
        if args.self_test:
            self_test()
            return 0
        store = SettingsStore(args.data_dir.resolve() if args.data_dir else None)
        settings = store.load()
        setup_logging(store.data_dir, settings)
        if args.check_environment:
            return 0 if environment_check(settings, store.data_dir) else 1
        if args.command == "settings":
            settings = set_preferences(store, settings, args.set) if args.set else settings
            print(json.dumps(settings.to_dict(), ensure_ascii=False, indent=2))
        elif args.command in {"history", "export"}:
            saved_command(args.command, store.data_dir)
        elif args.command == "process":
            changes = {key: getattr(args, key) for key in ("include_shorts", "include_members_only", "localization", "order")
                       if getattr(args, key) is not None}
            run_task(store.data_dir, replace(settings, **changes), "new", args.urls)
        elif args.command in {"resume", "update", "retry"}:
            run_task(store.data_dir, settings, args.command)
        else:
            return menu(store, settings)
        return 0
    except KeyboardInterrupt:
        print("\nОбработка прервана. Прогресс сохранён; используйте resume.")
        return 130
    except (UmdError, ValueError, OSError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
