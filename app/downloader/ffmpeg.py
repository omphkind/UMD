"""Hidden external media processing and interruptible subprocess ownership."""
from __future__ import annotations

from collections import deque
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time

from app.core.errors import ConfigurationError, MediaError
from app.downloader.ytdlp import YtDlp


class DownloadInterrupted(MediaError):
    def __init__(self, reason: str):
        super().__init__(reason, "Загрузка приостановлена." if reason == "paused" else "Загрузка отменена.")


def check_control(control) -> None:
    if control is None:
        return
    if control.cancel.is_set():
        raise DownloadInterrupted("cancelled")
    if control.pause.is_set():
        raise DownloadInterrupted("paused")


def terminate_process(process) -> None:
    """Terminate only our owned process tree, including its ffmpeg child."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            result = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           shell=False, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        process.kill()
        process.wait(timeout=5)


def run_process(args: list[str], *, on_line=None, control=None, timeout: float | None = None) -> tuple[int, str]:
    """A reader thread lets Pause/Cancel interrupt even silent network/ffmpeg work."""
    check_control(control)
    try:
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        raise ConfigurationError(f"Не удалось запустить {Path(args[0]).name}: {exc}") from exc
    lines: queue.Queue = queue.Queue()
    tail = deque(maxlen=30)

    def read():
        try:
            for line in process.stdout:
                lines.put(line.rstrip("\r\n"))
        finally:
            lines.put(None)

    reader = threading.Thread(target=read, name="umd-subprocess-output", daemon=True)
    reader.start()
    started = time.monotonic()
    ended = False
    try:
        while not ended:
            check_control(control)
            if timeout and time.monotonic() - started > timeout:
                raise MediaError("timeout", "Медиапроцесс превысил время ожидания.")
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                ended = True
            else:
                tail.append(line[-2000:])
                if on_line:
                    on_line(line)
        return_code = process.wait(timeout=10)
        check_control(control)
        return return_code, "\n".join(tail)[-6000:]
    finally:
        terminate_process(process)
        reader.join(timeout=2)
        if process.stdout:
            process.stdout.close()


class FFmpegProcessor:
    def __init__(self, settings, base_dir: str | Path | None = None):
        self.settings = settings
        self.backend = YtDlp(settings, base_dir=base_dir)

    @property
    def executable(self) -> str:
        return self.backend._executable(getattr(self.settings, "ffmpeg_path", ""), "ffmpeg")

    def check(self) -> str:
        try:
            result = subprocess.run([self.executable, "-version"], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", shell=False, timeout=15,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConfigurationError("FFmpeg не запускается. Проверьте путь в настройках.") from exc
        if result.returncode or not result.stdout.strip():
            raise ConfigurationError("FFmpeg не сообщил версию. Переустановите сборку UMD.")
        return result.stdout.splitlines()[0]

    def run(self, args: list[str], *, on_progress=None, control=None) -> None:
        def progress(line):
            if on_progress and line.startswith("out_time_us="):
                try:
                    elapsed = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    return
                on_progress({"stage": "processing", "elapsed": elapsed})
        rc, message = run_process([self.executable, "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", *args],
                                  on_line=progress, control=control)
        if rc:
            raise MediaError("postprocess", "FFmpeg не смог обработать медиа: " + message[-1600:])

    def merge(self, video: str | Path, audio: str | Path, output: str | Path, **kwargs):
        self.run(["-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", str(output)], **kwargs)

    def convert_audio(self, source: str | Path, output: str | Path, codec: str = "mp3", **kwargs):
        codecs = {"mp3": "libmp3lame", "m4a": "aac", "aac": "aac", "opus": "libopus", "wav": "pcm_s16le", "flac": "flac"}
        if codec not in codecs:
            raise ConfigurationError("Неподдерживаемый аудиоформат.")
        self.run(["-i", str(source), "-vn", "-c:a", codecs[codec], str(output)], **kwargs)

    def convert_subtitles(self, source: str | Path, output: str | Path, **kwargs):
        if Path(output).suffix.lower() not in {".srt", ".vtt", ".ass"}:
            raise ConfigurationError("Допустимые форматы субтитров: SRT, VTT, ASS.")
        self.run(["-i", str(source), str(output)], **kwargs)

    def remove_audio(self, source: str | Path, **kwargs):
        source = Path(source)
        temporary = source.with_name(source.stem + ".videoonly" + source.suffix)
        self.run(["-i", str(source), "-map", "0:v:0", "-c:v", "copy", "-an", str(temporary)], **kwargs)
        check_control(kwargs.get("control"))
        os.replace(temporary, source)
