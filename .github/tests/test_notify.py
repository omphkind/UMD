import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("notify", Path(__file__).parents[1] / "scripts" / "notify.py")
notify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notify)

BASE = {"NOTIFY_EVENT": "Публикация / очистка: beta", "NOTIFY_STATUS": "success",
        "GITHUB_ACTOR": "tester", "GITHUB_REPOSITORY": "omphkind/UMD", "GITHUB_RUN_ID": "123",
        "NOTIFY_DETAILS": "v0.1.0-beta.1",
        "NOTIFY_URL": "https://github.com/omphkind/UMD/releases/tag/v0.1.0-beta.1",
        "NOTIFY_DOWNLOAD_URL": "https://github.com/omphkind/UMD/releases/download/v0.1.0-beta.1/UMD-v0.1.0-beta.1-windows-x64.zip"}


class NotificationTests(unittest.TestCase):
    def test_release_includes_notes_and_direct_application_download(self):
        text = notify.notification_text(BASE, "- Исправлен экспорт.\n- Сохранён прогресс.")
        self.assertIn("- Исправлен экспорт.\n- Сохранён прогресс.", text)
        self.assertIn(BASE["NOTIFY_DOWNLOAD_URL"], text)
        self.assertIn(BASE["NOTIFY_URL"], text)

    def test_message_limit_preserves_download_and_run_links(self):
        text = notify.notification_text(BASE, "я" * 5000)
        self.assertLessEqual(len(text), 4096)
        self.assertIn(BASE["NOTIFY_DOWNLOAD_URL"], text)
        self.assertTrue(text.endswith("https://github.com/omphkind/UMD/actions/runs/123"))

    def test_main_reads_published_release_notes_for_telegram_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notes.md"
            path.write_text("- Обновлено приложение.\n", encoding="utf-8")
            environment = dict(BASE, NOTIFY_NOTES_FILE=str(path), TELEGRAM_BOT_TOKEN="test-token", TELEGRAM_CHAT_ID="test-chat")
            response = Mock()
            response.read.return_value = b'{"ok":true}'
            context = Mock()
            context.__enter__ = Mock(return_value=response)
            context.__exit__ = Mock(return_value=False)
            with patch.dict(os.environ, environment, clear=True), patch.object(notify.urllib.request, "urlopen", return_value=context) as send:
                notify.main()
            payload = json.loads(send.call_args.args[0].data)
            self.assertIn("- Обновлено приложение.", payload["text"])
            self.assertIn(BASE["NOTIFY_DOWNLOAD_URL"], payload["text"])

    def test_failed_publication_or_cleanup_does_not_include_release_fields(self):
        environment = dict(BASE, NOTIFY_STATUS="failure", NOTIFY_URL="", NOTIFY_DOWNLOAD_URL="")
        text = notify.notification_text(environment)
        self.assertNotIn("Скачать приложение", text)
        self.assertNotIn("Изменения:", text)


if __name__ == "__main__":
    unittest.main()
