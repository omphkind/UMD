"""Use the official local executable; never import yt-dlp or user config/plugins."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Protocol

from app.core.errors import ConfigurationError, MediaError

log = logging.getLogger(__name__)


class Downloader(Protocol):
    def check(self) -> str: ...
    def extract(self, url: str, *, flat: bool = False) -> dict: ...


def classify_error(message: str) -> str:
    """Keep access restrictions distinct from transient network/extractor failures."""
    text = message.lower()
    groups = (
        ("members_only", ("members-only", "member-only", "channel's members", "members only", "join this channel")),
        ("private", ("private video", "this video is private")),
        ("deleted", ("has been removed", "deleted video", "video has been deleted")),
        ("age_restricted", ("confirm your age", "age-restricted", "age restricted")),
        ("geo_restricted", ("not available in your country", "geo-restricted", "geographic restriction")),
        ("authentication_required", ("sign in", "login required", "authentication required", "use --cookies")),
        ("missing_js_runtime", ("no supported javascript runtime", "javascript runtime could not", "javascript runtime is required")),
        ("unsupported_url", ("unsupported url", "is not a valid url")),
        ("network", ("timed out", "timeout", "connection", "unable to download", "http error", "name resolution", "certificate verify")),
        ("unavailable", ("video unavailable", "not available", "unavailable video")),
    )
    for reason, hints in groups:
        if any(hint in text for hint in hints):
            return reason
    return "extractor"


class YtDlp:
    def __init__(self, settings, base_dir: str | Path | None = None, *, timeout: int = 180):
        self.settings = settings
        self.base_dir = Path(base_dir) if base_dir else (
            Path(sys.executable).parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[2]
        )
        self.timeout = timeout

    def _executable(self, configured: str, name: str, *, required: bool = True) -> str | None:
        if configured:
            path = Path(configured).expanduser()
            if not path.is_absolute():
                path = self.base_dir / path
            if not path.is_file():
                raise ConfigurationError(f"Не найден {name}: {path}")
            return str(path.resolve())
        filename = name + (".exe" if os.name == "nt" else "")
        for folder in (self.base_dir / "tools", self.base_dir):
            candidate = folder / filename
            if candidate.is_file():
                return str(candidate.resolve())
        candidate = shutil.which(filename)
        if candidate:
            return candidate
        if required:
            raise ConfigurationError(f"Не найден {name}. Укажите путь в настройках или поместите {filename} в tools.")
        return None

    def _run(self, args: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess:
        executable = self._executable(self.settings.yt_dlp_path, "yt-dlp")
        try:
            result = subprocess.run(
                [executable, "--ignore-config", "--no-plugin-dirs", *args],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                shell=False, timeout=timeout or self.timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired as exc:
            raise MediaError("network", "yt-dlp превысил время ожидания. Повторите обработку позже.") from exc
        except OSError as exc:
            raise ConfigurationError(f"Не удалось запустить yt-dlp: {exc}") from exc
        return result

    def check(self) -> str:
        result = self._run(["--version"], timeout=15)
        if result.returncode:
            raise ConfigurationError("yt-dlp не запускается. Проверьте исполняемый файл в настройках.")
        version = result.stdout.strip()
        if not version:
            raise ConfigurationError("yt-dlp не сообщил версию.")
        return version

    def runtime_version(self) -> str:
        executable = self._executable(getattr(self.settings, "deno_path", ""), "deno")
        try:
            result = subprocess.run(
                [executable, "--version"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", shell=False, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConfigurationError("Deno не запускается. Проверьте JavaScript runtime в tools или путь в настройках.") from exc
        if result.returncode or not result.stdout.strip():
            raise ConfigurationError("Deno не сообщил версию. Переустановите JavaScript runtime.")
        return result.stdout.strip().splitlines()[0]

    def extract(self, url: str, *, flat: bool = False) -> dict:
        args = ["--skip-download", "--dump-single-json", "--encoding", "utf-8",
                "--socket-timeout", "25", "--retries", "2", "--extractor-retries", "2",
                "--no-warnings", "--no-progress", "--no-remote-components"]
        deno = self._executable(getattr(self.settings, "deno_path", ""), "deno", required=False)
        if not deno:
            raise MediaError("missing_js_runtime", "Не найден Deno. Переустановите сборку UMD или укажите JavaScript runtime в настройках.")
        args.extend(["--js-runtimes", f"deno:{deno}"])
        args.extend(["--flat-playlist", "--ignore-errors"] if flat else ["--no-playlist"])
        # An argument terminator prevents a URL from ever becoming an option.
        result = self._run([*args, "--", url])
        if result.returncode and not (flat and result.stdout.strip()):
            message = result.stderr.strip() or "yt-dlp не смог получить данные."
            raise MediaError(classify_error(message), message[-1600:])
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise MediaError("extractor", "yt-dlp вернул некорректный JSON.") from exc
        if not isinstance(payload, dict):
            raise MediaError("extractor", "yt-dlp вернул неподдерживаемый ответ.")
        if result.returncode:
            log.warning("Часть списка источника недоступна: %s", result.stderr.strip()[-1600:])
        if self.settings.debug and result.stderr.strip():
            log.debug("yt-dlp: %s", result.stderr.strip()[-1600:])
        return payload
