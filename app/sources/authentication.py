"""Explicit authentication only: anonymous requests or a user chosen cookie file."""
from __future__ import annotations

from http.cookiejar import MozillaCookieJar
from pathlib import Path
import re
from urllib.request import HTTPCookieProcessor, build_opener

from app.core.errors import ConfigurationError


class AuthenticationManager:
    def __init__(self, settings):
        self.settings = settings

    @property
    def mode(self):
        return getattr(self.settings, "auth_mode", "anonymous")

    def cookie_file(self) -> str | None:
        if self.mode == "anonymous":
            return None
        if self.mode != "cookies":
            raise ConfigurationError("OAuth этого источника пока не подключён. Выберите анонимный доступ или cookie-файл.")
        value = getattr(self.settings, "cookie_file", "")
        if not value or not Path(value).expanduser().is_file():
            raise ConfigurationError("Выберите существующий Netscape cookie-файл в настройках авторизации.")
        return str(Path(value).expanduser().resolve())

    def arguments(self) -> list[str]:
        path = self.cookie_file()
        return ["--cookies", path] if path else []

    def opener(self):
        path = self.cookie_file()
        if not path:
            return build_opener()
        jar = MozillaCookieJar(path)
        try:
            jar.load(ignore_discard=True, ignore_expires=False)
        except (OSError, ValueError) as exc:
            raise ConfigurationError("Не удалось прочитать Netscape cookie-файл.") from exc
        return build_opener(HTTPCookieProcessor(jar))

    def redact(self, message: str) -> str:
        text = str(message)
        configured = getattr(self.settings, "cookie_file", "")
        if configured:
            text = text.replace(configured, "[cookie file]")
            text = text.replace(str(Path(configured).expanduser().resolve()), "[cookie file]")
        # Backend diagnostics must never expose session headers, tokens or URLs
        # containing signed credential parameters.
        text = re.sub(r"(?im)(cookie|authorization|set-cookie)\s*[:=]\s*[^\r\n]+", r"\1: [redacted]", text)
        return re.sub(r"(?i)(token|signature|password|sessionid|auth|key)=([^&\s]+)", r"\1=[redacted]", text)


def public_metadata(value):
    """Drop credential/header payloads before serializing backend metadata."""
    if isinstance(value, dict):
        return {str(k): public_metadata(v) for k, v in value.items()
                if not str(k).startswith("_") and str(k).lower() not in {
                    "cookies", "cookie", "http_headers", "headers", "authorization", "password", "access_token", "refresh_token"}}
    if isinstance(value, (tuple, list)):
        return [public_metadata(v) for v in value]
    return value if value is None or isinstance(value, (str, int, float, bool)) else str(value)
