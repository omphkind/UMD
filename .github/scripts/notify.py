"""Send CI lifecycle notifications without printing Telegram credentials."""

import json
import os
from pathlib import Path
import urllib.error
import urllib.request


def notification_text(environment, notes=""):
    header = (f"UMD | {environment['NOTIFY_EVENT']} | {environment['NOTIFY_STATUS']}\n"
              f"Автор: {environment['GITHUB_ACTOR']}")
    details = environment.get("NOTIFY_DETAILS", "")
    download_url = environment.get("NOTIFY_DOWNLOAD_URL", "")
    links = []
    if download_url:
        links.append(f"Скачать приложение Windows x64:\n{download_url}")
    if environment.get("NOTIFY_URL"):
        links.append(environment["NOTIFY_URL"])
    links.append(f"https://github.com/{environment['GITHUB_REPOSITORY']}/actions/runs/{environment['GITHUB_RUN_ID']}")
    footer = "\n\n".join(links)
    body = "\n\n".join(filter(None, [details, f"Изменения:\n{notes}" if notes else ""]))
    # Preserve the complete download link even when user-supplied details are long.
    budget = max(0, 4096 - len(header) - len(footer) - 4)
    return "\n\n".join(filter(None, [header, body[:budget], footer]))


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("::warning::Telegram secrets are missing; notification skipped")
        return
    notes = ""
    notes_file = os.environ.get("NOTIFY_NOTES_FILE", "")
    if notes_file and os.environ.get("NOTIFY_DOWNLOAD_URL"):
        try:
            notes = Path(notes_file).read_text(encoding="utf-8").strip()
        except OSError:
            print("::warning::Release notes could not be read for the Telegram notification")
    text = notification_text(os.environ, notes)
    payload = json.dumps({"chat_id": chat, "text": text,
                          "disable_web_page_preview": True}).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                     data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if not json.loads(response.read()).get("ok"):
                print("::warning::Telegram did not accept the notification")
    except (urllib.error.URLError, TimeoutError):
        # Do not print exceptions: they may contain the bot token in the URL.
        print("::warning::Telegram notification failed; inspect bot/chat access")


if __name__ == "__main__":
    main()
