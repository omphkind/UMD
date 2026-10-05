"""Translate validated GUI choices into resumable real yt-dlp downloads."""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
import json
import os
from pathlib import Path
import re
from typing import Callable

from app.core.errors import ConfigurationError, MediaError, SkipItem
from app.downloader.ffmpeg import FFmpegProcessor, DownloadInterrupted, check_control, run_process
from app.downloader.ytdlp import YtDlp, classify_error
from app.sources.resolver import has_audio, has_video, validate_url
from app.sources.authentication import AuthenticationManager
from app.storage.atomic import write_json

VIDEO_CONTAINERS = {"mp4", "mkv", "webm", "mov", "original"}
AUDIO_CONTAINERS = {"mp3", "m4a", "opus", "wav", "flac", "aac", "original"}
PHOTO_CONTAINERS = {"original", "jpg", "jpeg", "png", "webp", "gif", "bmp", "tiff", "tif", "avif", "heic", "heif", "psd"}


@dataclass
class DownloadOptions:
    media_type: str = "video"
    quality: str = "best"
    container: str = "mp4"
    audio: str = "with_audio"
    subtitles: str = "none"
    subtitle_format: str = "srt"
    thumbnail: bool = False
    chapters: bool = True
    output_path: str = ""
    metadata_preserve: bool = True
    embed_cover: bool = True
    audio_bitrate: str = "best"
    rename: dict = field(default_factory=dict)
    target_path: str = ""
    collision_policy: str = "ask"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None, settings=None):
        values = {
            "media_type": getattr(settings, "download_type", "video"),
            "quality": getattr(settings, "download_quality", "best"),
            "container": getattr(settings, "download_container", "mp4"),
            "audio": getattr(settings, "download_audio", "with_audio"),
            "subtitles": getattr(settings, "download_subtitles", "none"),
            "thumbnail": getattr(settings, "download_thumbnail", False),
            "output_path": getattr(settings, "output_path", ""),
            "metadata_preserve": getattr(settings, "metadata_preserve", True),
            "embed_cover": getattr(settings, "embed_cover", True),
            "audio_bitrate": getattr(settings, "audio_bitrate", "best"),
        }
        supplied = dict(data or {})
        if "type" in supplied:
            supplied["media_type"] = supplied.pop("type")
        unknown = set(supplied) - set(cls.__dataclass_fields__)
        if unknown:
            raise ConfigurationError("Неизвестные параметры загрузки: " + ", ".join(sorted(unknown)))
        values.update(supplied)
        result = cls(**values)
        if result.media_type not in {"video", "audio", "photo", "subtitles", "thumbnail", "metadata"}:
            raise ConfigurationError("Неподдерживаемый тип загрузки.")
        if result.audio not in {"with_audio", "video_only", "audio_only"}:
            raise ConfigurationError("Неподдерживаемый аудиорежим.")
        if not isinstance(result.quality, str) or not re.fullmatch(r"best|original|large|medium|small|[1-9][0-9]{1,4}p?|format:[A-Za-z0-9_.:-]+", result.quality):
            raise ConfigurationError("Качество должно быть best, высотой видео или реальным format ID.")
        if result.container not in VIDEO_CONTAINERS | AUDIO_CONTAINERS | PHOTO_CONTAINERS:
            raise ConfigurationError("Неподдерживаемый контейнер.")
        if not all(isinstance(value, bool) for value in (result.thumbnail, result.chapters, result.metadata_preserve, result.embed_cover)):
            raise ConfigurationError("Параметры обложки и глав должны быть логическими значениями.")
        if result.subtitle_format not in {"srt", "vtt", "ass"}:
            raise ConfigurationError("Неподдерживаемый формат субтитров.")
        if not isinstance(result.subtitles, str) or not isinstance(result.output_path, str):
            raise ConfigurationError("Язык субтитров и папка должны быть строками.")
        if not isinstance(result.target_path, str) or not isinstance(result.rename, dict):
            raise ConfigurationError("Параметры переименования должны содержать план и путь файла.")
        if result.collision_policy not in {"ask", "skip", "overwrite", "append", "append_number"}:
            raise ConfigurationError("Неподдерживаемое правило совпадения файлов.")
        if not isinstance(result.audio_bitrate, str) or not re.fullmatch(r"best|(?:32|64|96|128|160|192|224|256|320)k?", result.audio_bitrate):
            raise ConfigurationError("Допустимый битрейт: best или от 32 до 320 кбит/с.")
        return result


def resolve_target(options: DownloadOptions, default: Path) -> Path:
    """Revalidate the preview's path and collision policy immediately before I/O."""
    root = Path(options.output_path).expanduser().resolve()
    target = Path(options.target_path).expanduser().resolve() if options.target_path else default.resolve()
    if not target.is_relative_to(root) or target == root:
        raise ConfigurationError("Путь результата должен оставаться внутри выбранной папки загрузки.")
    if target.exists():
        if options.collision_policy == "overwrite":
            return target
        if options.collision_policy == "skip":
            raise SkipItem("collision")
        if options.collision_policy in {"append", "append_number"}:
            original = target
            number = 1
            while target.exists():
                target = original.with_name(f"{original.stem} ({number}){original.suffix}")
                number += 1
        else:
            raise MediaError("collision", "Файл уже существует. Выберите правило совпадения в предпросмотре переименования.")
    return target


def safe_filename(text: str) -> str:
    result = re.sub(r'[<>:"/\\|?*\x00-\x1f%]', "_", str(text))
    result = result.strip(" .")[:96].rstrip(" .") or "media"
    if result.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}}:
        result = "_" + result
    return result


def _number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_progress(line: str) -> dict | None:
    if line.startswith("UMD_PROGRESS:"):
        try:
            data = json.loads(line.partition(":")[2])
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        total = _number(data.get("total_bytes") or data.get("total_bytes_estimate"))
        downloaded = _number(data.get("downloaded_bytes"))
        percent = min(100.0, max(0.0, downloaded * 100 / total)) if total else 0.0
        return {"stage": "downloading", "progress": percent, "downloaded_bytes": downloaded,
                "total_bytes": total, "speed": _number(data.get("speed")), "eta": _number(data.get("eta"))}
    if line.startswith("UMD_POST:"):
        return {"stage": "processing"}
    return None


def subtitle_languages(analysis: dict, choice: str) -> list[str]:
    available = sorted(set(analysis.get("subtitles", {})) | set(analysis.get("automatic_captions", {})))
    if choice == "none":
        return []
    if choice == "all":
        result = available
    elif choice == "original":
        original = analysis.get("language")
        result = [lang for lang in available if lang == original or lang.endswith("-orig")]
        if not result and len(available) == 1:
            result = available
    else:
        result = [lang for lang in available if lang == choice or lang.startswith(choice + "-")]
    if not result:
        raise MediaError("subtitles_unavailable", "Выбранные субтитры недоступны для этого медиа.")
    return result


def _compatible(fmt: dict, container: str, *, audio: bool = False) -> bool:
    if container in {"mkv", "original"}:
        return True
    codec = str(fmt.get("acodec" if audio else "vcodec") or "")
    if not codec or codec == "none":
        return True
    prefixes = {
        "mp4": ("aac", "mp4a", "alac", "mp3") if audio else ("avc", "h264", "hevc", "h265", "hvc", "hev", "av01", "av1", "mpeg4", "mp4v"),
        "mov": ("aac", "mp4a", "alac", "pcm") if audio else ("avc", "h264", "hevc", "h265", "hvc", "hev", "mpeg4", "mp4v"),
        "webm": ("opus", "vorbis") if audio else ("vp8", "vp9", "vp09", "av01", "av1"),
    }
    return codec.startswith(prefixes.get(container, ()))


def original_audio_extension(fmt: dict) -> str | None:
    """The native audio codec determines -x's output, even inside an MP4/WebM."""
    codec = str(fmt.get("acodec") or "").lower()
    for prefixes, extension in ((("mp4a", "aac"), "m4a"), (("opus",), "opus"),
                                (("vorbis",), "ogg"), (("mp3",), "mp3"),
                                (("flac",), "flac"), (("pcm",), "wav")):
        if codec.startswith(prefixes):
            return extension
    extension = str(fmt.get("ext") or "").lower()
    return extension if extension in {"mp3", "m4a", "opus", "ogg", "wav", "flac", "aac"} and not has_video(fmt) else None


class YtDlpDownloader:
    def __init__(self, settings, base_dir: str | Path | None = None):
        self.settings = settings
        self.backend = YtDlp(settings, base_dir=base_dir)
        self.processor = FFmpegProcessor(settings, base_dir=base_dir)

    def _select_formats(self, analysis: dict, options: DownloadOptions) -> tuple[str, bool, bool, bool]:
        """Return selector, ffmpeg-needed, recode-needed, strip-audio-needed."""
        formats = analysis.get("formats") or []
        video = [fmt for fmt in formats if has_video(fmt) and not fmt.get("has_drm")]
        audio = [fmt for fmt in formats if has_audio(fmt) and not fmt.get("has_drm")]
        if options.media_type == "audio" or options.audio == "audio_only":
            if options.quality.startswith("format:"):
                identifier = options.quality.removeprefix("format:")
                audio = [fmt for fmt in audio if str(fmt.get("format_id")) == identifier]
            if not audio:
                raise MediaError("format_unavailable", "Аудиодорожка недоступна.")
            if options.container == "mp4":
                options.container = "m4a"
            if options.container not in AUDIO_CONTAINERS:
                raise ConfigurationError("Выберите аудиоконтейнер MP3, M4A, Opus, WAV или FLAC.")
            chosen = max(audio, key=lambda fmt: (not has_video(fmt), _number(fmt.get("abr")), _number(fmt.get("tbr"))))
            return str(chosen["format_id"]), True, False, False
        if options.container not in VIDEO_CONTAINERS:
            raise ConfigurationError("Для видео нужен видеоконтейнер.")
        if options.quality.startswith("format:"):
            identifier = options.quality.removeprefix("format:")
            video = [fmt for fmt in video if str(fmt["format_id"]) == identifier]
        elif options.quality != "best":
            height = int(options.quality.rstrip("p"))
            video = [fmt for fmt in video if _number(fmt.get("height")) == height]
        if not video:
            raise MediaError("format_unavailable", "Выбранное качество отсутствует в доступных форматах. Выполните анализ заново.")
        chosen = max(video, key=lambda fmt: (_number(fmt.get("height")), _compatible(fmt, options.container),
                                           not has_audio(fmt) if options.audio == "video_only" else False,
                                           _number(fmt.get("fps")), _number(fmt.get("tbr"))))
        selector = str(chosen["format_id"])
        merge = False
        recode = not _compatible(chosen, options.container)
        strip = options.audio == "video_only" and has_audio(chosen)
        if options.audio == "with_audio":
            if has_audio(chosen):
                recode |= not _compatible(chosen, options.container, audio=True)
            else:
                if not audio:
                    raise MediaError("format_unavailable", "У источника нет аудиодорожки. Выберите «Видео без звука».")
                audio_only = [fmt for fmt in audio if not has_video(fmt)] or audio
                sound = max(audio_only, key=lambda fmt: (_compatible(fmt, options.container, audio=True), _number(fmt.get("abr")), _number(fmt.get("tbr"))))
                selector += "+" + str(sound["format_id"])
                merge = True
                recode |= not _compatible(sound, options.container, audio=True)
        remux = options.container != "original" and chosen.get("ext") != options.container
        return selector, merge or recode or remux or strip, recode, strip

    def build_command(self, analysis: dict, options: DownloadOptions) -> tuple[list[str], Path, str, bool]:
        url = validate_url(analysis.get("url", ""))
        if analysis.get("entries") or analysis.get("media_type") == "playlist":
            raise ConfigurationError("Плейлист должен быть развёрнут в отдельные задачи очереди.")
        output = Path(options.output_path).expanduser()
        if not options.output_path:
            raise ConfigurationError("Выберите папку для загрузки.")
        output = output.resolve()
        prefix = safe_filename(analysis.get("title") or "media") + " [" + safe_filename(str(analysis.get("source") or "media") + "-" + str(analysis.get("id") or analysis.get("media_id") or "media"))[:48] + "]"
        if options.media_type == "video":
            prefix += " [" + safe_filename(options.quality + "-" + options.audio)[:40] + "]"
        elif options.media_type == "audio":
            prefix += " [audio-" + safe_filename(options.quality)[:40] + "]"
        if options.media_type == "audio" and options.container == "mp4":
            options.container = "m4a"
        if options.target_path:
            planned = Path(options.target_path).expanduser().resolve()
            if not planned.is_relative_to(output):
                raise ConfigurationError("Результат переименования должен находиться в папке загрузки.")
            output, prefix = planned.parent, planned.stem
        executable = self.backend._executable(self.settings.yt_dlp_path, "yt-dlp")
        args = [executable, "--ignore-config", "--no-plugin-dirs", "--no-remote-components", "--no-playlist",
                "--encoding", "utf-8", "--socket-timeout", "25", "--retries", "3", "--fragment-retries", "3",
                "--continue", "--part", "--no-overwrites", "--windows-filenames", "--newline", "--no-colors",
                "--progress", "--progress-delta", "0.4", "--no-simulate",
                "--progress-template", "download:UMD_PROGRESS:%(progress)j",
                "--progress-template", "postprocess:UMD_POST:%(progress)j",
                "--print", "after_move:UMD_FILE:%(filepath)j",
                "--output", str(output / prefix).replace("%", "%%") + ".%(ext)s"]
        args.extend(AuthenticationManager(self.settings).arguments())
        if options.collision_policy == "overwrite":
            args[args.index("--no-overwrites")] = "--force-overwrites"
        # Generic HTML pages can contain several videos at the same webpage URL.
        # Keep each queued entry bound to its original playlist position.
        index = analysis.get("playlist_index")
        if analysis.get("playlist_url") == url and isinstance(index, int) and index > 0:
            args.extend(["--playlist-items", str(index)])
        deno = self.backend._executable(getattr(self.settings, "deno_path", ""), "deno", required=False)
        if deno:
            args.extend(["--js-runtimes", f"deno:{deno}"])
        elif analysis.get("source") == "youtube":
            raise MediaError("missing_js_runtime", "Для YouTube нужен Deno. Проверьте runtime в настройках.")
        requires_ffmpeg = False
        strip = False
        if options.media_type in {"video", "audio"}:
            selector, requires_ffmpeg, recode, strip = self._select_formats(analysis, options)
            if options.target_path:
                planned_ext = Path(options.target_path).suffix.lstrip(".").lower()
                chosen = next((f for f in analysis.get("formats", []) if str(f.get("format_id")) == selector.split("+")[0]), {})
                expected_ext = chosen.get("ext") if options.container == "original" else options.container
                if options.container == "original" and (options.media_type == "audio" or options.audio == "audio_only"):
                    expected_ext = original_audio_extension(chosen)
                    if expected_ext is None:
                        raise ConfigurationError("Оригинальный аудиокодек не определён. Для точного имени выберите MP3, M4A, Opus, WAV или FLAC.")
                if options.container == "original" and "+" in selector:
                    # yt-dlp can choose MKV when source codecs cannot share a
                    # container. Such a path cannot be truthfully previewed.
                    raise ConfigurationError("Для точного имени объединённого видео выберите MP4, MKV или WebM вместо original.")
                if expected_ext and planned_ext != str(expected_ext).lower():
                    raise ConfigurationError("Формат файла изменился после анализа. Обновите предпросмотр имени или выберите явный контейнер.")
            args.extend(["--format", selector])
            if options.media_type == "audio" or options.audio == "audio_only":
                bitrate = "0" if options.audio_bitrate == "best" else options.audio_bitrate.rstrip("k") + "K"
                args.extend(["--extract-audio", "--audio-format", "best" if options.container == "original" else options.container, "--audio-quality", bitrate])
                if options.metadata_preserve:
                    args.append("--embed-metadata")
                if options.embed_cover and (analysis.get("thumbnail") or analysis.get("thumbnails")):
                    if options.container in {"wav", "aac"}:
                        # These containers cannot represent an attached picture;
                        # preserve the cover beside the track instead.
                        args.append("--write-thumbnail")
                    else:
                        args.extend(["--embed-thumbnail", "--convert-thumbnails", "jpg"])
            elif options.container != "original":
                args.extend(["--merge-output-format", "mkv" if recode else options.container])
                args.extend(["--recode-video" if recode else "--remux-video", options.container])
        else:
            args.append("--skip-download")
        if options.media_type == "thumbnail" or options.thumbnail:
            if not analysis.get("thumbnail") and not analysis.get("thumbnails"):
                if options.media_type == "thumbnail":
                    raise MediaError("thumbnail_unavailable", "У источника нет обложки.")
            else:
                args.append("--write-thumbnail")
                if options.media_type == "thumbnail" and options.target_path:
                    target_format = Path(options.target_path).suffix.lstrip(".").lower()
                    if target_format not in {"jpg", "png", "webp"}:
                        raise ConfigurationError("Итоговый формат обложки должен быть JPG, PNG или WebP.")
                    args.extend(["--convert-thumbnails", target_format])
                    requires_ffmpeg = True
        languages = subtitle_languages(analysis, options.subtitles)
        if options.media_type == "subtitles" and not languages:
            raise ConfigurationError("Выберите язык субтитров.")
        if options.media_type == "subtitles" and options.target_path and len(languages) != 1:
            raise ConfigurationError("Для точного имени субтитров выберите один язык; несколько языков сохраняются с суффиксами языка без переименования.")
        if languages:
            args.extend(["--write-subs", "--write-auto-subs", "--sub-langs", ",".join(re.escape(lang) for lang in languages),
                         "--sub-format", "vtt/best"])
            if options.subtitle_format != "vtt":
                args.extend(["--convert-subs", options.subtitle_format])
                requires_ffmpeg = True
        if options.chapters and analysis.get("chapters") and options.media_type in {"video", "audio"}:
            args.append("--embed-chapters")
            requires_ffmpeg = True
        if requires_ffmpeg:
            args.extend(["--ffmpeg-location", self.processor.executable])
        return [*args, "--", url], output, prefix, strip

    def download(self, analysis: dict, options: dict | DownloadOptions | None = None,
                 on_progress: Callable | None = None, control=None) -> dict:
        check_control(control)
        if analysis.get("is_shorts") and not self.settings.include_shorts:
            raise SkipItem("shorts")
        if analysis.get("is_members_only") and not self.settings.include_members_only:
            raise SkipItem("members_only")
        selection = options if isinstance(options, DownloadOptions) else DownloadOptions.from_dict(options, self.settings)
        # Validate even objects supplied by a UI service.
        selection = DownloadOptions.from_dict(selection.to_dict(), self.settings)
        if on_progress:
            on_progress({"stage": "preparing", "progress": 0.0, "speed": 0.0, "eta": 0.0})
        args, output, prefix, strip = self.build_command(analysis, selection)
        if selection.target_path:
            target = resolve_target(selection, Path(selection.target_path))
            if target != Path(selection.target_path).expanduser().resolve():
                selection.target_path = str(target)
                args, output, prefix, strip = self.build_command(analysis, selection)
        try:
            output.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigurationError(f"Не удалось открыть папку загрузки: {exc}") from exc
        files = []
        sidecar_prefix = prefix if selection.target_path else prefix + "." + selection.media_type
        metadata_path = Path(selection.target_path).resolve() if selection.media_type == "metadata" and selection.target_path else output / (sidecar_prefix + ".umd.json")
        if selection.metadata_preserve or selection.media_type == "metadata":
            metadata_selection = DownloadOptions.from_dict({**selection.to_dict(), "target_path": str(metadata_path)})
            metadata_path = resolve_target(metadata_selection, metadata_path)
        chapters_file = None
        if selection.chapters and analysis.get("chapters"):
            chapters_file = output / (sidecar_prefix + ".chapters.json")
            chapter_selection = DownloadOptions.from_dict({**selection.to_dict(), "target_path": str(chapters_file)})
            chapters_file = resolve_target(chapter_selection, chapters_file)

        def observe(line: str):
            progress = parse_progress(line)
            if progress and on_progress:
                on_progress(progress)
            if line.startswith("UMD_FILE:"):
                try:
                    path = Path(json.loads(line.partition(":")[2]))
                    if path.is_file() and path.resolve().parent == output and str(path) not in files:
                        files.append(str(path))
                except (ValueError, TypeError):
                    pass

        if selection.media_type != "metadata":
            code, stderr = run_process(args, on_line=observe, control=control)
            if code:
                reason = classify_error(stderr)
                if reason == "members_only" and not self.settings.include_members_only:
                    raise SkipItem("members_only")
                if any(hint in stderr.lower() for hint in ("ffmpeg", "conversion failed", "postprocessing")):
                    reason = "postprocess"
                raise MediaError(reason, AuthenticationManager(self.settings).redact(stderr[-1800:]) or "Загрузчик завершился с ошибкой.")
            check_control(control)
            for path in output.iterdir():
                temporary = re.search(r"\.(?:videoonly|temp|f[0-9]+)\.", path.name[len(prefix):])
                if path.name.startswith(prefix + ".") and path.is_file() and path.suffix not in {".part", ".ytdl", ".temp", ".json"} and ".part-" not in path.name and not temporary:
                    if str(path) not in files:
                        files.append(str(path))
            if not files:
                raise MediaError("download_empty", "Загрузчик не создал медиафайлы. Проверьте доступность выбранного формата.")
            if selection.media_type == "subtitles" and selection.target_path:
                target = Path(selection.target_path).resolve()
                candidates = [Path(file) for file in files if Path(file).suffix == "." + selection.subtitle_format]
                if len(candidates) != 1:
                    raise MediaError("download_empty", "Загрузчик не создал единственный выбранный файл субтитров.")
                original = candidates[0]
                if original != target:
                    if target.exists() and selection.collision_policy != "overwrite":
                        raise MediaError("collision", "Итоговый файл появился во время загрузки; исходные субтитры сохранены.")
                    # yt-dlp mandates a language suffix. Its removal is the
                    # backend's required finalization, using the previewed name.
                    os.replace(original, target)
                    files[files.index(str(original))] = str(target)
            if strip:
                if on_progress:
                    on_progress({"stage": "processing"})
                for file in files:
                    if Path(file).suffix.removeprefix(".") in VIDEO_CONTAINERS:
                        self.processor.remove_audio(file, on_progress=on_progress, control=control)
        check_control(control)
        if selection.metadata_preserve or selection.media_type == "metadata":
            write_json(metadata_path, {"analysis": analysis, "download_options": selection.to_dict(), "files": files})
            files.append(str(metadata_path))
        if chapters_file is not None:
            write_json(chapters_file, {"chapters": analysis["chapters"]})
            files.append(str(chapters_file))
        if on_progress:
            on_progress({"stage": "completed", "progress": 100.0, "speed": 0.0, "eta": 0.0, "files": files})
        return {"files": files, "metadata": analysis}


def download(settings, analysis: dict, options=None, on_progress=None, control=None) -> dict:
    from app.downloader.router import create_downloader
    return create_downloader(settings, analysis).download(analysis, options, on_progress, control)
