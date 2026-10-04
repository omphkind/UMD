import pytest

from app.core.config import Settings
from app.localization.youtube import YouTubeLocalizer, check_browser


@pytest.fixture(scope="module")
def localizer():
    diagnostics = check_browser()
    if not diagnostics["ok"]:
        pytest.skip(diagnostics["message"])
    with YouTubeLocalizer(Settings()) as browser:
        yield browser


def test_description_is_scoped_not_neighboring_ui(localizer):
    page = localizer._usable_page()
    page.set_content('''<html><body>
      <div>Recommended video / subscribers / views</div>
      <ytd-watch-metadata>
        <h1><yt-formatted-string>Русское название 30 minutes</yt-formatted-string></h1>
        <div id="description"><div>Creator 1 million views</div>
          <div id="description-inline-expander"><div id="expanded"><yt-attributed-string style="white-space:pre-line">Первая строка\n\nВторая строка</yt-attributed-string></div></div>
        </div>
      </ytd-watch-metadata>
      <div id="expanded">Unrelated expanded UI</div>
    </body></html>''')
    result = localizer.extract_page(page)
    assert result["title"] == "Русское название 30 minutes"
    assert result["description"] == "Первая строка\n\nВторая строка"
    assert "description-inline-expander" in result["description_selector"]


def test_expand_description_and_legacy_fallback(localizer):
    page = localizer._usable_page()
    page.set_content('''<div id="primary"><div id="info"><h1 class="title"><yt-formatted-string>Old layout</yt-formatted-string></h1></div></div>
      <ytd-video-secondary-info-renderer>
        <button id="more" onclick="document.querySelector('#description').style.display='block';this.style.display='none'">Show more</button>
        <div id="description" style="display:none"><yt-formatted-string>Actual description</yt-formatted-string></div>
      </ytd-video-secondary-info-renderer><div>Nearby UI text</div>''')
    result = localizer.extract_page(page)
    assert result["title"] == "Old layout"
    assert result["description"] == "Actual description"


def test_changed_unknown_dom_returns_empty_without_contamination(localizer):
    page = localizer._usable_page()
    page.set_content("<body><h1>Unknown title layout</h1><div id='expanded'>Subscribers and recommendations</div></body>")
    result = localizer.extract_page(page)
    assert result["title"] == result["description"] == ""


def test_closed_page_recreated_in_same_browser_and_locale_preserved(localizer):
    browser, context = localizer._browser, localizer._context
    page = localizer._usable_page()
    page.close()
    replacement = localizer._usable_page()
    assert replacement is not page and not replacement.is_closed()
    assert localizer._browser is browser and localizer._context is context
    replacement.set_content("<html><body>Locale test</body></html>")
    assert replacement.evaluate("navigator.language") == "ru-RU"


def test_collapsed_snippet_never_extracted_when_full_description_unavailable(localizer):
    page = localizer._usable_page()
    page.set_content('''<ytd-watch-metadata><h1><yt-formatted-string>Title</yt-formatted-string></h1>
      <div id="description-inline-expander"><div id="expanded" style="display:none"><yt-attributed-string>Full text</yt-attributed-string></div>
      <div id="snippet"><yt-attributed-string>Truncated snippet...</yt-attributed-string></div></div></ytd-watch-metadata>''')
    result = localizer.extract_page(page)
    assert result["description"] == ""
    assert result["description_found"] is False


def test_expanded_content_waits_for_delayed_dom_update(localizer):
    page = localizer._usable_page()
    page.set_content('''<ytd-watch-metadata><h1><yt-formatted-string>Title</yt-formatted-string></h1>
      <div id="description-inline-expander"><button id="expand" onclick="setTimeout(()=>{document.querySelector('#expanded').style.display='block';this.style.display='none'}, 50)">Show more</button>
      <div id="expanded" style="display:none"><yt-attributed-string>Complete description</yt-attributed-string></div>
      <div id="snippet"><yt-attributed-string>Truncated...</yt-attributed-string></div></div></ytd-watch-metadata>''')
    result = localizer.extract_page(page)
    assert result["description"] == "Complete description"


def test_consent_visible_text_differs_from_aria_label(localizer):
    page = localizer._usable_page()
    page.set_content('''<div role="dialog"><button aria-label="Запретить использование файлов cookie и данных" onclick="this.parentElement.remove()">Отклонить все</button>
      <button aria-label="Разрешить использование файлов cookie и данных" onclick="document.body.dataset.accepted='yes'">Принять все</button></div>''')
    localizer._dismiss_consent(page)
    assert page.get_by_role("dialog").count() == 0
    assert page.locator("body").get_attribute("data-accepted") is None
