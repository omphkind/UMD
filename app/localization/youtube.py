"""Anonymous YouTube DOM extraction with one browser/context per processing run."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import re

from app.core.errors import ConfigurationError, MediaError

log = logging.getLogger(__name__)

TITLE_SELECTORS = (
    "ytd-watch-metadata h1 yt-formatted-string",
    "ytd-watch-metadata h1 yt-attributed-string",
    "#primary #info h1.title yt-formatted-string",
    "ytd-reel-video-renderer[is-active] h2 yt-formatted-string",
    "ytd-reel-video-renderer[is-active] .ytReelPlayerOverlayViewModelTitle",
)
DESCRIPTION_SELECTORS = (
    "ytd-watch-metadata #description-inline-expander #expanded > yt-attributed-string",
    "ytd-watch-metadata #description-inline-expander #expanded > yt-formatted-string",
    "ytd-watch-metadata #description-inline-expander yt-formatted-string#description-text",
    "ytd-watch-metadata #description ytd-text-inline-expander #expanded",
    "ytd-watch-metadata #description #expanded yt-formatted-string.content",
    "ytd-video-secondary-info-renderer #description yt-formatted-string",
    'ytd-engagement-panel-section-list-renderer[target-id="engagement-panel-shorts-description"] #description yt-attributed-string',
)
EXPAND_SELECTORS = (
    "ytd-watch-metadata #description-inline-expander #expand",
    "ytd-watch-metadata #description #expand",
    "ytd-watch-metadata #description #more",
    "ytd-video-secondary-info-renderer #more",
)


def _browser_path_environment():
    # Bundled Playwright stores Chromium inside its own package. Developers may
    # override this with a pre-existing installation via the documented env var.
    if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
        try:
            import playwright
            local = Path(playwright.__file__).parent / "driver" / "package" / ".local-browsers"
            if local.is_dir():
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
        except ImportError:
            pass


class YouTubeLocalizer:
    def __init__(self, settings):
        self.settings = settings
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def start(self):
        if self._browser is not None and self._browser.is_connected():
            return self
        self.close()
        _browser_path_environment()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ConfigurationError("Playwright отсутствует. Переустановите сборку UMD; для исходников: pip install playwright.") from exc
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
            self._context = self._browser.new_context(
                locale=self.settings.locale,
                extra_http_headers={"Accept-Language": f"{self.settings.locale},{self.settings.language};q=0.9"},
            )
            self._context.set_default_timeout(2500)
            self._page = self._context.new_page()
        except Exception as exc:
            self.close()
            raise ConfigurationError("Chromium не удалось запустить. Для исходников выполните python -m playwright install chromium; для готовой сборки переустановите UMD.") from exc
        return self

    def close(self):
        # Independently close every owned resource even after a browser crash.
        for name in ("_page", "_context", "_browser", "_playwright"):
            resource = getattr(self, name)
            setattr(self, name, None)
            if resource is not None:
                try:
                    resource.stop() if name == "_playwright" else resource.close()
                except Exception:
                    log.debug("Resource already closed: %s", name, exc_info=True)

    def _usable_page(self):
        if self._browser is None or not self._browser.is_connected():
            self.start()
        if self._page is None or self._page.is_closed():
            self._page = self._context.new_page()
        return self._page

    def _dismiss_consent(self, page):
        # YouTube's visible reject text differs from its accessibility label.
        # Match the actual button text inside a dialog; never accept tracking.
        reject = page.get_by_role("dialog").locator("button").filter(
            has_text=re.compile(r"^\s*(Reject all|Отклонить все|Отклонить всё)\s*$", re.I)
        )
        if reject.count() and reject.first.is_visible():
            reject.first.click(timeout=5000)
            reject.first.wait_for(state="hidden", timeout=5000)

        # The separate consent domain may use submit inputs rather than dialog.
        standalone = page.get_by_role("button", name=re.compile(r"^(Reject all|Отклонить все|Отклонить всё)$", re.I))
        if standalone.count() and standalone.first.is_visible():
            standalone.first.click(timeout=5000)

    def extract_page(self, page) -> dict:
        """Extract only dedicated metadata nodes. Also used by offline DOM tests."""
        for selector in EXPAND_SELECTORS:
            element = page.locator(selector).first
            try:
                if element.count() and element.is_visible():
                    element.click(timeout=2000)
                    expanded = page.locator("ytd-watch-metadata #description-inline-expander #expanded").first
                    if expanded.count():
                        expanded.wait_for(state="visible", timeout=3000)
                    if self.settings.debug:
                        log.debug("Description expand selector: %s", selector)
                    break
            except Exception:
                if self.settings.debug:
                    log.debug("Description expand fallback: %s", selector)

        def read(selectors: tuple[str, ...]):
            found = False
            for selector in selectors:
                nodes = page.locator(selector)
                try:
                    for index in range(min(nodes.count(), 4)):
                        element = nodes.nth(index)
                        if element.is_visible():
                            found = True
                            value = element.inner_text(timeout=2000).strip()
                            if value:
                                if self.settings.debug:
                                    log.debug("Metadata selector: %s", selector)
                                return value, selector, True
                except Exception:
                    if self.settings.debug:
                        log.debug("Metadata selector fallback: %s", selector)
            return "", "", found

        title, title_selector, _ = read(TITLE_SELECTORS)
        description, description_selector, description_found = read(DESCRIPTION_SELECTORS)
        return {"title": title, "description": description,
                "title_selector": title_selector, "description_selector": description_selector,
                "description_found": description_found}

    def fetch(self, url: str) -> dict:
        from app.sources.youtube import parse_youtube_url
        resource = parse_youtube_url(url)
        if resource.kind != "video":
            raise MediaError("unsupported_url", "Локализация требует URL отдельного видео.")
        page = self._usable_page()
        try:
            page.goto(resource.url + f"&hl={self.settings.language}", wait_until="domcontentloaded", timeout=45000)
            # Consent is dismissed anonymously. No user browser profile/cookies.
            self._dismiss_consent(page)
            page.locator(", ".join(TITLE_SELECTORS)).first.wait_for(state="visible", timeout=20000)
            # Wait for asynchronously rendered metadata before expanding it.
            try:
                page.locator("ytd-watch-metadata #description-inline-expander, ytd-video-secondary-info-renderer #description").first.wait_for(state="visible", timeout=5000)
            except Exception:
                pass
            self._dismiss_consent(page)
            if self.settings.debug:
                log.debug("Localization url=%s page=%s title=%s", url, page.url, page.title())
            data = self.extract_page(page)
            if not data["title"]:
                raise MediaError("localization", "Название YouTube не найдено в DOM.")
            return data
        except MediaError:
            raise
        except Exception as exc:
            raise MediaError("localization", "Не удалось получить локализованные данные YouTube; используются оригинальные метаданные.") from exc


def check_browser(settings=None) -> dict:
    """Offline launch/navigation verifies an executable browser, not just a file."""
    if settings is None:
        from app.core.config import Settings
        settings = Settings()
    try:
        with YouTubeLocalizer(settings) as localizer:
            page = localizer._usable_page()
            page.set_content("<html><body><p id='diagnostic'>UMD browser OK</p></body></html>")
            if page.locator("#diagnostic").inner_text() != "UMD browser OK":
                raise ConfigurationError("Chromium не смог прочитать тестовую страницу.")
        return {"ok": True, "message": "Playwright и Chromium доступны", "playwright": True, "chromium": True, "details": "Playwright и Chromium доступны"}
    except ConfigurationError as exc:
        try:
            import playwright  # noqa: F401
            installed = True
        except ImportError:
            installed = False
        return {"ok": False, "message": str(exc), "playwright": installed, "chromium": False, "details": str(exc)}
