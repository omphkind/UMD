"""Translate extractor metadata into the choices shown by the desktop UI."""


def video_formats(analysis):
    if "video_formats" in analysis:
        return analysis["video_formats"]
    return [f for f in analysis.get("formats", [])
            if f.get("vcodec") not in (None, "none") or f.get("height")]


def audio_formats(analysis):
    if "audio_formats" in analysis:
        return analysis["audio_formats"]
    return [f for f in analysis.get("formats", [])
            if f.get("acodec") not in (None, "none")]


def quality_choices(analysis, kind="video", advanced=False):
    formats = audio_formats(analysis) if kind == "audio" else video_formats(analysis)
    if not formats:
        return []
    choices = [("Лучшее доступное", "best")]
    if kind == "video":
        heights = sorted({int(f["height"]) for f in formats if f.get("height")}, reverse=True)
        choices += [(f"{height}p", str(height)) for height in heights]
    if advanced:
        for f in reversed(formats):
            identifier = str(f.get("format_id", ""))
            if not identifier:
                continue
            resolution = f.get("resolution") or (f"{f['height']}p" if f.get("height") else "аудио")
            size = f.get("filesize") or f.get("filesize_approx")
            detail = " · ".join(str(x) for x in (
                resolution, f.get("fps") and f"{f['fps']} fps", f.get("vcodec"),
                f.get("acodec"), f.get("tbr") and f"{f['tbr']:.0f} kbps",
                f.get("dynamic_range"), size and f"{size / 1024**2:.1f} MB", f"ID {identifier}"
            ) if x and x != "none")
            choices.append((detail, "format:" + identifier))
    return choices


def subtitle_choices(analysis):
    manual = analysis.get("subtitles") or {}
    automatic = analysis.get("automatic_captions") or {}
    languages = sorted(set(manual) | set(automatic), key=lambda x: (x not in ("ru", "en"), x))
    choices = [("Без субтитров", "none")]
    if languages:
        choices.append(("Все доступные", "all"))
    for language in languages:
        label = {"ru": "Русский", "en": "English"}.get(language, language)
        label += " · авторские" if language in manual else " · автоматически"
        choices.append((label, language))
    return choices


def duration_text(value):
    if value is None:
        return "Длительность неизвестна"
    seconds = int(value)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def bytes_text(value):
    if not value:
        return "—"
    value = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
