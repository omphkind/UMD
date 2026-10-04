"""Conservative, deterministic cleanup; original text stays in MediaItem."""
from __future__ import annotations

import re
from urllib.parse import urlparse

EMAIL = re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.I)
URL = re.compile(r"https?://[^\s<>]+", re.I)
SOCIAL_HOSTS = {"instagram.com", "tiktok.com", "facebook.com", "fb.com", "t.me", "telegram.me", "twitter.com", "x.com", "vk.com", "discord.gg", "discord.com", "linktr.ee"}
DURATION = re.compile(
    r"(?:\d+\s*(?:hours?|minutes?|seconds?|час(?:а|ов)?|минут(?:а|ы)?|секунд(?:а|ы)?)(?:\s*[,;]\s*|\s+)?)+$", re.I
)
AD_LINE = re.compile(r"^(?:sponsored by|sponsor(?:ed)?\s*:|paid promotion\s*:|реклама\s*:|спонсор\s*:|промокод\s*:|promo(?:tion)? code\s*:|for business inquiries\b|для (?:рекламы|сотрудничества)\s*:|business (?:email|contact)\s*:)", re.I)
CTA_LINE = re.compile(r"^(?:follow me(?: on)?|subscribe(?: to my)?|подпишись|подписывайтесь|мои соцсети|social(?: media)? links)\s*[:!\-]?\s*(?:https?://|instagram\b|tiktok\b|facebook\b|telegram\b|vk\b|[\w@])", re.I)
TECHNICAL_LINE = re.compile(r"^(?:[a-z0-9]+(?:\.[a-z0-9]+)*\s*[=:]\s*|(?:tags?|теги|keywords|ключевые слова)\s*:)" , re.I)
HASHTAGS_ONLY = re.compile(r"(?:#[\w]+\s*)+\Z", re.UNICODE)


def clean_title(text: str | None, *, from_aria_label: bool = False, original_title: str | None = None) -> str:
    title = re.sub(r"\s+", " ", text or "").strip()
    # Only aria-label is known to append duration. Genuine titles such as
    # "Learn Python in 30 minutes" must survive ordinary normalization.
    if from_aria_label:
        if original_title:
            original = clean_title(original_title)
            if title == original:
                return original
            if title.startswith(original) and DURATION.fullmatch(title[len(original):].strip(" ,;-")):
                return original
        match = DURATION.search(title)
        if match:
            title = title[:match.start()].rstrip(" ,;-")
    return title


def _social_url(match: re.Match) -> str:
    value = match.group()
    hostname = (urlparse(value.rstrip(".,;!)")).hostname or "").lower().removeprefix("www.")
    return "" if any(hostname == host or hostname.endswith("." + host) for host in SOCIAL_HOSTS) else value


def clean_description(text: str | None) -> str:
    lines = []
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        # A marker is insufficient to delete a whole following paragraph: remove
        # only an explicit advertising/technical line and preserve narrative.
        if AD_LINE.match(line):
            continue
        social_link = any(_social_url(match) == "" for match in URL.finditer(line))
        if CTA_LINE.match(line) and (social_link or re.search(r"\b(?:instagram|tiktok|facebook|telegram|vk)\b", line, re.I)):
            continue
        if TECHNICAL_LINE.match(line) and (re.match(r"^(?:tags?|теги|keywords|ключевые слова)\s*:", line, re.I) or re.match(r"^(?:video_id|codec|bitrate|resolution|generated_by)\s*[=:]", line, re.I)):
            continue
        line = EMAIL.sub("", line)
        line = URL.sub(_social_url, line)
        # Drop the now-empty label of a removed social/contact link only.
        if re.fullmatch(r"(?:Instagram|TikTok|Facebook|Telegram|Twitter|VK|Email|E-mail|Contact|Почта|Контакт)\s*[:\-]?\s*", line, re.I):
            continue
        line = re.sub(r"[ \t]{2,}", " ", line).strip()
        if line or (lines and lines[-1]):
            lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    while lines and HASHTAGS_ONLY.fullmatch(lines[-1]):
        lines.pop()
        while lines and not lines[-1]:
            lines.pop()
    return "\n".join(lines).strip()
