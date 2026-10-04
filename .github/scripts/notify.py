"""Send CI lifecycle notifications without printing Telegram credentials."""

import json
import os
import urllib.error
import urllib.request


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("::warning::Telegram secrets are missing; notification skipped")
        return
    text = "\n".join(filter(None, [
        f"UMD | {os.environ['NOTIFY_EVENT']} | {os.environ['NOTIFY_STATUS']}",
        f"Автор: {os.environ['GITHUB_ACTOR']}",
        os.environ.get("NOTIFY_DETAILS", ""),
        os.environ.get("NOTIFY_URL", ""),
        f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
    ]))[:4096]
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
