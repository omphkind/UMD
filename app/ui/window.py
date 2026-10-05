"""Qt desktop shell. Network, extraction and downloads live in backend workers."""
from __future__ import annotations

from dataclasses import replace
import io
import json
import logging
from contextlib import redirect_stdout
from pathlib import Path
import threading

from PySide6.QtCore import Qt, QTimer, QUrl, QSize, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from app import __version__
from app.core.config import Settings, SettingsStore
from app.core.errors import UmdError
from app.storage.progress import ProgressStore
from app.tasks.queue import QueueService
from app.ui.application import run_task
from app.ui.formats import audio_formats, bytes_text, duration_text, quality_choices, subtitle_choices, video_formats
from app.ui.theme import STYLE

log = logging.getLogger(__name__)
STATUS_LABELS = {"Queued": "В очереди", "Analyzing": "Анализ", "Downloading": "Загрузка",
                 "Processing": "Обработка", "Completed": "Готово", "Failed": "Ошибка",
                 "Paused": "Пауза", "Cancelled": "Отменено"}


def button(text, callback, primary=False):
    widget = QPushButton(text)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        widget.setObjectName("primary")
    widget.clicked.connect(callback)
    return widget


def label(text="", name=None):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        widget.setObjectName(name)
    return widget


def combo(choices=()):
    widget = QComboBox()
    for title, value in choices:
        widget.addItem(title, value)
    return widget


def select(widget, value):
    index = widget.findData(value)
    if index >= 0:
        widget.setCurrentIndex(index)


def logo():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#547cef"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, 60, 60, 15, 15)
    painter.setPen(QColor("white"))
    painter.setFont(QFont("Segoe UI", 30, QFont.Weight.Bold))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "↓")
    painter.end()
    return QIcon(pixmap)


class MainWindow(QMainWindow):
    backend_event = Signal(object)

    def __init__(self, store: SettingsStore, queue_service=None):
        super().__init__()
        self.store = store
        self.settings = store.load()
        self.queue = queue_service or QueueService(self.settings, store.data_dir)
        self.analysis = None
        self.analysis_request = None
        self.analyzed_urls = []
        self.metadata_cancel = threading.Event()
        self.metadata_thread = None
        self.closing = False
        self._rows_signature = None
        self.network = QNetworkAccessManager(self)
        self.thumbnail_reply = None
        self.queue_thumbnails = {}
        self.pending_thumbnails = set()
        self.setWindowTitle(f"UMD — Universal Media Downloader · {__version__}")
        self.setWindowIcon(logo())
        self.resize(1200, 850)
        self.setMinimumSize(1000, 730)
        self.setStyleSheet(STYLE)
        shell = QWidget()
        layout = QHBoxLayout(shell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(190)
        navigation = QVBoxLayout(sidebar)
        navigation.setContentsMargins(20, 26, 20, 20)
        navigation.addWidget(label("UMD", "brand"))
        subtitle = label("Universal Media\nDownloader", "muted")
        navigation.addWidget(subtitle)
        navigation.addSpacing(32)
        self.pages = QStackedWidget()
        self.nav = []
        self.page_names = ["Загрузки", "Очередь", "Библиотека", "История", "Настройки"]
        for index, name in enumerate(self.page_names):
            nav = button(name, lambda checked=False, i=index: self.switch_page(i))
            nav.setObjectName("nav")
            nav.setCheckable(True)
            navigation.addWidget(nav)
            self.nav.append(nav)
        navigation.addStretch()
        navigation.addWidget(label("WINDOWS X64", "muted"))
        navigation.addWidget(label(__version__, "badge"))
        layout.addWidget(sidebar)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(shell)
        self.build_downloads()
        self.build_queue()
        self.build_library()
        self.build_history()
        self.build_settings()
        self.backend_event.connect(self.handle_backend_event)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(250)
        self.switch_page(0)
        self.statusBar().showMessage("Вставьте ссылку и нажмите «Анализировать»")
        self.refresh_queue()
        self.refresh_history()
        QTimer.singleShot(300, self.quick_environment_check)

    def page(self, title, description):
        page = QWidget()
        content = QVBoxLayout(page)
        content.setContentsMargins(28, 24, 28, 24)
        content.setSpacing(14)
        content.addWidget(label(title, "title"))
        content.addWidget(label(description, "muted"))
        self.pages.addWidget(page)
        return content

    def card(self):
        card = QFrame()
        card.setObjectName("card")
        return card

    def switch_page(self, index):
        self.pages.setCurrentIndex(index)
        for i, nav in enumerate(self.nav):
            nav.setChecked(i == index)
        if index in (2, 3):
            self.refresh_history()

    def build_downloads(self):
        content = self.page("Новая загрузка", "Сначала анализ ссылки — затем выбор формата и добавление в очередь.")
        input_row = QHBoxLayout()
        self.url = QPlainTextEdit()
        self.url.setObjectName("urlInput")
        self.url.setAccessibleName("Ссылки для анализа")
        self.url.setPlaceholderText("Вставьте URL видео, канала или плейлиста · несколько ссылок — по одной в строке")
        self.url.setMaximumHeight(62)
        input_row.addWidget(self.url, 1)
        input_row.addWidget(button("Вставить", self.paste_url))
        self.analyze_button = button("Анализировать", self.analyze, True)
        input_row.addWidget(self.analyze_button)
        content.addLayout(input_row)
        self.analysis_status = label("Источник определяется автоматически через yt-dlp.", "muted")
        self.analysis_status.setWordWrap(True)
        content.addWidget(self.analysis_status)
        preview = self.card()
        preview_row = QHBoxLayout(preview)
        preview_row.setContentsMargins(16, 16, 16, 16)
        self.thumbnail = label("UMD\nПредпросмотр")
        self.thumbnail.setFixedSize(210, 118)
        self.thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail.setStyleSheet("background: #101a2c; border-radius: 8px; color: #8aa5de;")
        preview_row.addWidget(self.thumbnail)
        details = QVBoxLayout()
        self.media_title = label("Добавьте ссылку для анализа")
        self.media_title.setWordWrap(True)
        self.media_title.setStyleSheet("font-size: 18px; font-weight: 600;")
        details.addWidget(self.media_title)
        self.original_title = label("", "muted")
        self.original_title.setWordWrap(True)
        details.addWidget(self.original_title)
        self.media_details = label("Видео · Аудио · Субтитры · Обложки · Метаданные", "muted")
        self.media_details.setWordWrap(True)
        details.addWidget(self.media_details)
        self.media_extra = label("", "muted")
        self.media_extra.setWordWrap(True)
        details.addWidget(self.media_extra)
        details.addStretch()
        preview_row.addLayout(details, 1)
        content.addWidget(preview)
        options = self.card()
        grid = QGridLayout(options)
        grid.setContentsMargins(18, 15, 18, 15)
        grid.setHorizontalSpacing(18)
        self.kind = combo([("Видео", "video"), ("Аудио", "audio"), ("Субтитры", "subtitles"),
                           ("Обложка", "thumbnail"), ("Только метаданные", "metadata")])
        self.quality = combo()
        self.container = combo()
        self.audio = combo([("С аудио", "with_audio"), ("Только видео", "video_only")])
        self.subtitles = combo([("Без субтитров", "none")])
        self.subtitle_format = combo([("VTT", "vtt"), ("SRT", "srt"), ("ASS", "ass")])
        for col, (name, widget) in enumerate((("Тип", self.kind), ("Качество", self.quality), ("Контейнер", self.container))):
            grid.addWidget(label(name, "muted"), 0, col)
            grid.addWidget(widget, 1, col)
        for col, (name, widget) in enumerate((("Аудио", self.audio), ("Субтитры", self.subtitles), ("Формат субтитров", self.subtitle_format))):
            grid.addWidget(label(name, "muted"), 2, col)
            grid.addWidget(widget, 3, col)
        checks = QHBoxLayout()
        self.save_thumbnail = QCheckBox("Сохранить обложку")
        self.save_thumbnail.setChecked(self.settings.download_thumbnail)
        self.save_chapters = QCheckBox("Сохранить главы")
        self.save_chapters.setChecked(True)
        self.advanced = QCheckBox("Подробные форматы")
        checks.addWidget(self.save_thumbnail)
        checks.addWidget(self.save_chapters)
        checks.addWidget(self.advanced)
        grid.addLayout(checks, 4, 0, 1, 3)
        content.addWidget(options)
        select(self.kind, self.settings.download_type)
        self.kind.currentIndexChanged.connect(self.refresh_choices)
        self.advanced.toggled.connect(self.refresh_choices)
        self.subtitles.currentIndexChanged.connect(self.refresh_subtitle_format)
        self.refresh_choices()
        output_row = QHBoxLayout()
        output_row.addWidget(label("Сохранить в", "muted"))
        self.output = QLineEdit(self.settings.output_path)
        self.output.setAccessibleName("Каталог загрузки")
        output_row.addWidget(self.output, 1)
        output_row.addWidget(button("Обзор…", lambda: self.browse_directory(self.output)))
        self.download_button = button("Добавить в очередь", self.download, True)
        self.download_button.setEnabled(False)
        output_row.addWidget(self.download_button)
        content.addLayout(output_row)
        self.url.textChanged.connect(self.invalidate_analysis)
        content.addStretch()
        content.addWidget(label("YouTube · Vimeo · Twitch · TikTok · другие источники, доступные yt-dlp", "muted"))

    def refresh_subtitle_format(self):
        self.subtitle_format.setEnabled(self.kind.currentData() == "subtitles" or self.subtitles.currentData() != "none")

    def refresh_choices(self, *_):
        kind = self.kind.currentData()
        analysis = self.analysis or {}
        previous = self.quality.currentData() or self.settings.download_quality
        self.quality.clear()
        for title, value in quality_choices(analysis, kind, self.advanced.isChecked()):
            self.quality.addItem(title, value)
        if analysis.get("entries") and not self.quality.count() and kind in {"video", "audio"}:
            self.quality.addItem("Лучшее для каждого объекта", "best")
        select(self.quality, previous)
        self.quality.setEnabled(kind in {"video", "audio"} and self.quality.count() > 0)
        previous_container = self.container.currentData() or self.settings.download_container
        self.container.clear()
        containers = ["original", "mp3", "m4a", "opus", "wav", "flac"] if kind == "audio" else ["mp4", "mkv", "webm", "mov", "original"]
        for value in containers:
            self.container.addItem("Исходный формат" if value == "original" else value.upper(), value)
        select(self.container, previous_container)
        self.container.setEnabled(kind in {"video", "audio"})
        self.audio.setEnabled(kind == "video")
        select(self.audio, self.settings.download_audio)
        previous_subs = self.subtitles.currentData() or self.settings.download_subtitles
        self.subtitles.clear()
        for title, value in subtitle_choices(analysis):
            self.subtitles.addItem(title, value)
        if analysis.get("entries") and self.subtitles.findData("all") < 0:
            self.subtitles.addItem("Все доступные для каждого объекта", "all")
        select(self.subtitles, previous_subs)
        self.refresh_subtitle_format()
        self.save_thumbnail.setEnabled(bool(analysis.get("thumbnail")))
        self.save_chapters.setEnabled(bool(analysis.get("chapters")))
        collection = bool(analysis.get("entries"))
        available = bool(self.analysis) and (
            kind == "metadata" or collection or
            (kind == "video" and video_formats(analysis)) or
            (kind == "audio" and audio_formats(analysis)) or
            (kind == "subtitles" and self.subtitles.count() > 1) or
            (kind == "thumbnail" and analysis.get("thumbnail")))
        if hasattr(self, "download_button"):
            self.download_button.setEnabled(bool(available))

    def paste_url(self):
        self.url.setPlainText(QApplication.clipboard().text().strip())

    def input_urls(self):
        return [value.strip() for value in self.url.toPlainText().splitlines() if value.strip()]

    def invalidate_analysis(self):
        if self.input_urls() != self.analyzed_urls:
            self.analysis = None
            self.download_button.setEnabled(False)
            if self.analysis_request:
                self.queue.cancel_analysis(self.analysis_request)
                self.analysis_request = None
                self.analyze_button.setEnabled(True)
            self.analysis_status.setText("Ссылка изменена. Выполните анализ перед загрузкой.")

    def analyze(self):
        urls = self.input_urls()
        if not urls:
            self.statusBar().showMessage("Вставьте хотя бы одну ссылку.")
            self.url.setFocus()
            return
        self.analysis = None
        self.analyzed_urls = list(urls)
        self.download_button.setEnabled(False)
        self.analyze_button.setEnabled(False)
        self.analysis_status.setText("Анализируем источник и доступные форматы…")
        try:
            self.analysis_request = self.queue.analyze(urls[0])
        except (UmdError, ValueError, OSError) as error:
            self.analysis_error(str(error))

    def analysis_error(self, message):
        self.analyze_button.setEnabled(True)
        self.analysis_status.setText("Не удалось проанализировать ссылку: " + message)
        self.statusBar().showMessage("Анализ не завершён. Проверьте ссылку или повторите позже.")

    def show_analysis(self, analysis):
        self.analysis = analysis
        self.analyze_button.setEnabled(True)
        self.media_title.setText(str(analysis.get("title") or analysis.get("id") or "Медиаресурс"))
        original = analysis.get("original_title")
        self.original_title.setText("Оригинал: " + original if original and original != analysis.get("title") else "")
        author = analysis.get("channel") or analysis.get("uploader") or analysis.get("author") or "Автор не указан"
        entries = analysis.get("entries") or []
        detail = [str(analysis.get("source") or analysis.get("extractor") or "yt-dlp"), str(author)]
        detail.append(f"{len(entries)} объектов" if entries else duration_text(analysis.get("duration")))
        if analysis.get("upload_date") or analysis.get("published_at"):
            detail.append(str(analysis.get("upload_date") or analysis.get("published_at")))
        self.media_details.setText(" · ".join(detail))
        self.media_extra.setText(f"Видео: {len(video_formats(analysis))} форматов · Аудио: {len(audio_formats(analysis))} форматов · "
                                 f"Главы: {len(analysis.get('chapters') or [])}")
        self.analysis_status.setText("Источник определён. Выберите параметры загрузки." if not entries else
                                    "Список найден. Каждый объект будет отдельно проверен перед загрузкой; качество зависит от объекта.")
        self.refresh_choices()
        self.load_thumbnail(analysis.get("thumbnail"))

    def load_thumbnail(self, url):
        if self.thumbnail_reply:
            self.thumbnail_reply.abort()
        self.thumbnail.clear()
        self.thumbnail.setText("UMD\nПредпросмотр")
        if not url or QUrl(url).scheme() not in {"http", "https"}:
            return
        reply = self.network.get(QNetworkRequest(QUrl(url)))
        self.thumbnail_reply = reply
        reply.downloadProgress.connect(lambda size, total: reply.abort() if size > 8_000_000 else None)
        QTimer.singleShot(15000, reply, reply.abort)
        reply.finished.connect(lambda: self.thumbnail_finished(reply))

    def thumbnail_finished(self, reply):
        if reply is self.thumbnail_reply:
            image = QPixmap()
            if reply.error() == reply.NetworkError.NoError and image.loadFromData(reply.readAll()):
                self.thumbnail.setPixmap(image.scaled(self.thumbnail.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                     Qt.TransformationMode.SmoothTransformation))
            self.thumbnail_reply = None
        reply.deleteLater()

    def download(self):
        if not self.analysis:
            return
        urls = self.input_urls()
        if not self.output.text().strip():
            self.statusBar().showMessage("Укажите каталог загрузки.")
            return
        options = {"media_type": self.kind.currentData(), "quality": self.quality.currentData() or "best",
                   "container": self.container.currentData(), "audio": self.audio.currentData(),
                   "subtitles": self.subtitles.currentData(), "subtitle_format": self.subtitle_format.currentData(),
                   "thumbnail": self.save_thumbnail.isChecked(), "chapters": self.save_chapters.isChecked(),
                   "output_path": self.output.text().strip()}
        try:
            if options["media_type"] == "metadata" and str(self.analysis.get("source", "")).lower() == "youtube":
                self.run_metadata("new", urls)
                self.switch_page(3)
                return
            tasks = self.queue.enqueue(self.analysis, options)
            # Additional URLs are resolved in the queue worker, not on the UI thread.
            for url in urls[1:]:
                self.queue.enqueue({"url": url, "webpage_url": url, "id": url, "source": "unresolved",
                                    "title": url, "needs_analysis": True}, options)
            self.statusBar().showMessage(f"Добавлено задач: {len(tasks) + max(0, len(urls) - 1)}")
            self.switch_page(1)
            self.refresh_queue()
        except (UmdError, ValueError, OSError) as error:
            self.statusBar().showMessage(str(error))

    def build_queue(self):
        content = self.page("Очередь загрузок", "Задачи сохраняются автоматически. После перезапуска незавершённые задачи доступны для продолжения.")
        all_actions = QHBoxLayout()
        for title, action in (("Пауза всех", self.queue.pause), ("Продолжить все", self.queue.resume),
                              ("Отменить все", self.queue.cancel), ("Повторить ошибки", self.queue.retry_failed),
                              ("Убрать готовые", self.queue.clear_completed)):
            all_actions.addWidget(button(title, lambda checked=False, fn=action: self.queue_action(fn)))
        content.addLayout(all_actions)
        self.queue_table = QTableWidget(0, 8)
        self.queue_table.setObjectName("downloadQueue")
        self.queue_table.setHorizontalHeaderLabels(["Название / источник", "Формат", "Статус", "Прогресс", "Скорость", "ETA", "Этап", "Ошибка"])
        self.queue_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.queue_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.queue_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.queue_table.setIconSize(QSize(72, 40))
        self.queue_table.verticalHeader().hide()
        self.queue_table.setColumnWidth(1, 150)
        self.queue_table.setColumnWidth(2, 100)
        self.queue_table.setColumnWidth(3, 105)
        self.queue_table.setColumnWidth(4, 90)
        self.queue_table.setColumnWidth(5, 60)
        self.queue_table.setColumnWidth(6, 90)
        self.queue_table.setColumnWidth(7, 120)
        content.addWidget(self.queue_table, 1)
        actions = QHBoxLayout()
        for title, method in (("Пауза", "pause"), ("Продолжить", "resume"), ("Отменить", "cancel")):
            actions.addWidget(button(title, lambda checked=False, name=method: self.selected_action(name)))
        actions.addWidget(button("Повторить", self.retry_selected))
        actions.addWidget(button("Открыть папку", self.open_selected_folder))
        actions.addWidget(button("Копировать URL", self.copy_selected_url))
        content.addLayout(actions)
        self.queue_summary = label("", "muted")
        content.addWidget(self.queue_summary)

    def queue_action(self, fn, *args):
        try:
            fn(*args)
            self.refresh_queue(force=True)
        except (UmdError, ValueError, OSError) as error:
            self.statusBar().showMessage(str(error))

    def selected_task(self):
        row = self.queue_table.currentRow()
        if row < 0 or not self.queue_table.item(row, 0):
            self.statusBar().showMessage("Выберите задачу в очереди.")
            return None
        task_id = self.queue_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        return next((task for task in self.queue.snapshot(compact=True) if task["id"] == task_id), None)

    def selected_action(self, method):
        task = self.selected_task()
        if task:
            self.queue_action(getattr(self.queue, method), task["id"])

    def retry_selected(self):
        task = self.selected_task()
        if task:
            self.queue_action(self.queue.retry, task["id"])

    def open_selected_folder(self):
        task = self.selected_task()
        if task:
            folder = Path(task["files"][0]).parent if task.get("files") else Path(task.get("options", {}).get("output_path") or self.output.text())
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.resolve())))

    def copy_selected_url(self):
        task = self.selected_task()
        if task:
            QApplication.clipboard().setText(task["url"])
            self.statusBar().showMessage("URL скопирован.")

    def refresh_queue(self, force=False):
        tasks = self.queue.snapshot(compact=True)
        signature = json.dumps(tasks, ensure_ascii=False, sort_keys=True, default=str)
        if signature == self._rows_signature and not force:
            return
        self._rows_signature = signature
        selected = None
        row = self.queue_table.currentRow()
        if row >= 0 and self.queue_table.item(row, 0):
            selected = self.queue_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        self.queue_table.setRowCount(len(tasks))
        counts = {}
        for row, task in enumerate(tasks):
            status = task["status"]
            counts[status] = counts.get(status, 0) + 1
            opts = task.get("options") or {}
            quality = opts.get("quality", "best")
            format_label = f"{opts.get('media_type', '')} · {'best' if quality == 'best' else quality} · {opts.get('container', '')}\n{opts.get('audio', '')}"
            values = [f"{task.get('title') or task['url']}\n{task.get('source', '')}", format_label,
                      STATUS_LABELS.get(status, status), "", bytes_text(task.get("speed")) + "/s" if task.get("speed") else "—",
                      duration_text(task["eta"]) if task.get("eta") is not None else "—", task.get("stage", ""), task.get("error") or ""]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                self.queue_table.setItem(row, col, item)
            first = self.queue_table.item(row, 0)
            first.setData(Qt.ItemDataRole.UserRole, task["id"])
            thumbnail = task.get("thumbnail") or task.get("analysis", {}).get("thumbnail")
            if thumbnail in self.queue_thumbnails:
                first.setIcon(self.queue_thumbnails[thumbnail])
            elif thumbnail:
                self.fetch_queue_thumbnail(thumbnail)
            if status == "Failed":
                self.queue_table.item(row, 2).setForeground(QColor("#ff9a9a"))
            elif status == "Completed":
                self.queue_table.item(row, 2).setForeground(QColor("#88dfb8"))
            progress = QProgressBar()
            value = task.get("progress") or 0
            progress.setRange(0, 100)
            progress.setValue(min(100, max(0, int(value))))
            self.queue_table.setCellWidget(row, 3, progress)
            self.queue_table.setRowHeight(row, 58)
            if selected == task["id"]:
                self.queue_table.selectRow(row)
        self.queue_summary.setText(" · ".join(f"{STATUS_LABELS.get(key, key)}: {value}" for key, value in counts.items()) or "Очередь пуста")
        self.refresh_library(tasks)

    def fetch_queue_thumbnail(self, url):
        if url in self.pending_thumbnails or len(self.pending_thumbnails) >= 4 or QUrl(url).scheme() not in {"http", "https"}:
            return
        self.pending_thumbnails.add(url)
        reply = self.network.get(QNetworkRequest(QUrl(url)))
        reply.downloadProgress.connect(lambda size, total: reply.abort() if size > 8_000_000 else None)
        QTimer.singleShot(15000, reply, reply.abort)
        def finished():
            image = QPixmap()
            icon = QIcon()
            if reply.error() == reply.NetworkError.NoError and image.loadFromData(reply.readAll()):
                icon = QIcon(image)
            self.queue_thumbnails[url] = icon
            self.pending_thumbnails.discard(url)
            self._rows_signature = None
            reply.deleteLater()
        reply.finished.connect(finished)

    def build_library(self):
        content = self.page("Библиотека", "Готовые загрузки и сохранённые файлы текущей очереди.")
        self.library = QTableWidget(0, 3)
        self.library.setHorizontalHeaderLabels(["Название", "Источник", "Файлы"])
        self.library.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.library.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.library.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.library.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.library.cellDoubleClicked.connect(self.open_library_file)
        content.addWidget(self.library)
        content.addWidget(button("Открыть каталог загрузок", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.output.text()))))

    def refresh_library(self, tasks):
        completed = [task for task in tasks if task["status"] == "Completed"]
        self.library.setRowCount(len(completed))
        for row, task in enumerate(completed):
            for col, value in enumerate((task.get("title"), task.get("source"), "\n".join(task.get("files") or []))):
                self.library.setItem(row, col, QTableWidgetItem(value or ""))

    def open_library_file(self, row, _col):
        files = self.library.item(row, 2).text().splitlines()
        if files:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(files[0]).parent)))

    def build_history(self):
        content = self.page("История и метаданные", "Обработка YouTube с постоянной нумерацией, локализацией и экспортом MetaFin / JSON.")
        actions = QHBoxLayout()
        for title, operation in (("Новая обработка", "new"), ("Продолжить", "resume"),
                                 ("Обновить источник", "update"), ("Повторить ошибки", "retry")):
            actions.addWidget(button(title, lambda checked=False, op=operation: self.run_metadata(op, self.input_urls() if op == "new" else None)))
        actions.addWidget(button("Остановить", self.metadata_cancel.set))
        content.addLayout(actions)
        self.history_summary = label("", "muted")
        self.history_summary.setWordWrap(True)
        content.addWidget(self.history_summary)
        self.history_text = QPlainTextEdit()
        self.history_text.setReadOnly(True)
        content.addWidget(self.history_text, 1)
        content.addWidget(button("Открыть экспорт", self.open_metadata_output))

    def open_metadata_output(self):
        try:
            progress = ProgressStore(self.store.data_dir / "UMD_PROGRESS.json").load()
            path = progress.settings.get("output_path") if progress else self.output.text()
            QDesktopServices.openUrl(QUrl.fromLocalFile(path or str(self.store.data_dir / "output")))
        except UmdError as error:
            self.statusBar().showMessage(str(error))

    def run_metadata(self, operation, urls=None):
        if self.metadata_thread and self.metadata_thread.is_alive():
            self.statusBar().showMessage("Обработка метаданных уже идёт.")
            return
        if operation == "new" and not urls:
            self.switch_page(0)
            self.statusBar().showMessage("Сначала добавьте YouTube URL на экране загрузок.")
            return
        self.metadata_cancel.clear()
        if operation == "new":
            self.history_text.clear()
        self.history_summary.setText("Получаем список объектов и метаданные…")
        settings = replace(self.settings, output_path=self.output.text().strip() or self.settings.output_path)
        self.statusBar().showMessage("Обработка метаданных…")
        def work():
            try:
                run_task(self.store.data_dir, settings, operation, urls,
                         on_event=self.backend_event.emit, cancel_event=self.metadata_cancel, quiet=True)
            except KeyboardInterrupt:
                pass  # Backend already saved pending items and emitted interrupted.
            except Exception as error:
                log.exception("Metadata task failed")
                self.backend_event.emit({"type": "metadata_error", "message": str(error)})
            finally:
                self.backend_event.emit({"type": "metadata_finished"})
        self.metadata_thread = threading.Thread(target=work, name="UMD-metadata", daemon=True)
        self.metadata_thread.start()

    def refresh_history(self):
        if not hasattr(self, "history_summary"):
            return
        try:
            progress = ProgressStore(self.store.data_dir / "UMD_PROGRESS.json").load()
            if not progress:
                self.history_summary.setText("Сохранённой обработки пока нет. URL берутся с экрана «Загрузки».")
                return
            self.history_summary.setText(" · ".join(f"{key}: {value}" for key, value in progress.counts.items()))
            lines = ["Источники: " + ", ".join(progress.source_urls), "", json.dumps(progress.history, ensure_ascii=False, indent=2), ""]
            for item in sorted(progress.items.values(), key=lambda value: value.number):
                lines += [f"[{item.number:03d}] {item.status} · {item.title or item.media_id}",
                          item.error_reason or item.skip_reason or item.url]
            self.history_text.setPlainText("\n".join(lines))
        except UmdError as error:
            self.history_summary.setText(str(error))

    def build_settings(self):
        content = self.page("Настройки", "Сохранённые предпочтения для новых задач. Активные задачи сохраняют свои параметры.")
        scroller = QScrollArea()
        scroller.setWidgetResizable(True)
        body = QWidget()
        form = QFormLayout(body)
        form.setSpacing(14)
        self.setting_fields = {}
        for name, title in (("yt_dlp_path", "Путь к yt-dlp"), ("deno_path", "Путь к Deno"),
                            ("ffmpeg_path", "Путь к ffmpeg"), ("output_path", "Каталог загрузок"),
                            ("locale", "Локаль браузера"), ("language", "Язык")):
            field = QLineEdit(getattr(self.settings, name))
            field.setAccessibleName(title)
            if name.endswith("path"):
                row = QWidget()
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.addWidget(field, 1)
                browse = (lambda checked=False, w=field: self.browse_directory(w)) if name == "output_path" else (lambda checked=False, w=field: self.browse_tool(w))
                row_layout.addWidget(button("Обзор…", browse))
                form.addRow(title, row)
                if name != "output_path":
                    field.setPlaceholderText("Автоматически: встроенный инструмент")
            else:
                form.addRow(title, field)
            self.setting_fields[name] = field
        for name, title in (("include_shorts", "Включать Shorts"), ("include_members_only", "Включать Members-only"),
                            ("localization", "Получать локализацию YouTube"), ("debug", "Подробный лог")):
            field = QCheckBox(title)
            field.setChecked(getattr(self.settings, name))
            form.addRow("", field)
            self.setting_fields[name] = field
        self.order = combo([("От старых к новым", "oldest_first"), ("От новых к старым", "newest_first")])
        select(self.order, self.settings.order)
        form.addRow("Порядок метаданных", self.order)
        form.addRow("", label("Параметры форматов на экране загрузки сохраняются как значения по умолчанию.", "muted"))
        scroller.setWidget(body)
        content.addWidget(scroller, 1)
        actions = QHBoxLayout()
        actions.addWidget(button("Сохранить настройки", self.save_settings, True))
        actions.addWidget(button("Проверить окружение", self.environment_check))
        actions.addWidget(button("Открыть логи", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.data_dir / "logs")))))
        content.addLayout(actions)
        self.environment_result = QPlainTextEdit()
        self.environment_result.setReadOnly(True)
        self.environment_result.setMaximumHeight(145)
        content.addWidget(self.environment_result)

    def browse_directory(self, field):
        value = QFileDialog.getExistingDirectory(self, "Каталог сохранения", field.text())
        if value:
            field.setText(value)

    def browse_tool(self, field):
        value, _ = QFileDialog.getOpenFileName(self, "Исполняемый файл", field.text(), "Приложения (*.exe);;Все файлы (*)")
        if value:
            field.setText(value)

    def save_settings(self):
        values = self.settings.to_dict()
        for name, field in self.setting_fields.items():
            values[name] = field.isChecked() if isinstance(field, QCheckBox) else field.text().strip()
        values.update(order=self.order.currentData(), download_type=self.kind.currentData(),
                      download_quality=self.quality.currentData() or "best", download_container=self.container.currentData(),
                      download_audio=self.audio.currentData(), download_subtitles=self.subtitles.currentData(),
                      download_thumbnail=self.save_thumbnail.isChecked())
        try:
            self.settings = Settings.from_dict(values)
            self.store.save(self.settings)
            self.queue.settings = self.settings
            self.output.setText(self.settings.output_path)
            self.statusBar().showMessage("Настройки сохранены.")
        except (UmdError, OSError) as error:
            self.statusBar().showMessage(str(error))

    def environment_check(self):
        self.environment_result.setPlainText("Проверяем инструменты и Chromium…")
        settings = self.settings
        def work():
            # diagnostics prints are captured only within this dedicated check;
            # the user interface itself never relies on terminal output.
            from app.ui.diagnostics import environment_check
            buffer = io.StringIO()
            try:
                with redirect_stdout(buffer):
                    ok = environment_check(settings, self.store.data_dir)
                self.backend_event.emit({"type": "environment", "ok": ok, "text": buffer.getvalue()})
            except Exception as error:
                self.backend_event.emit({"type": "environment", "ok": False, "text": str(error)})
        threading.Thread(target=work, name="UMD-environment", daemon=True).start()

    def quick_environment_check(self):
        def work():
            try:
                from app.downloader.ytdlp import YtDlp
                from app.downloader.ffmpeg import FFmpegProcessor
                versions = ["yt-dlp " + YtDlp(self.settings).check(), YtDlp(self.settings).runtime_version()]
                FFmpegProcessor(self.settings).check()
                self.backend_event.emit({"type": "tools_ready", "text": " · ".join(versions) + " · ffmpeg OK"})
            except Exception as error:
                self.backend_event.emit({"type": "tools_error", "text": str(error)})
        threading.Thread(target=work, name="UMD-tools", daemon=True).start()

    @Slot(object)
    def handle_backend_event(self, event):
        if self.closing:
            return
        kind = event.get("type")
        if kind == "analysis_ready" and event.get("request_id") == self.analysis_request:
            self.show_analysis(event["analysis"])
        elif kind == "analysis_failed" and event.get("request_id") == self.analysis_request:
            self.analysis_error(event.get("error") or "Источник недоступен")
        elif kind == "item":
            item = event["item"]
            self.history_summary.setText(" · ".join(f"{key}: {value}" for key, value in event.get("counts", {}).items()))
            self.history_text.appendPlainText(f"[{item['number']:03d}] {item['status']} · {item.get('title') or item['media_id']}")
            self.statusBar().showMessage("Прогресс обработки сохранён.")
        elif kind == "completed":
            self.statusBar().showMessage("Метаданные сохранены.")
        elif kind in {"interrupted", "metadata_finished"}:
            self.refresh_history()
            self.statusBar().showMessage("Прогресс обработки сохранён.")
        elif kind in {"warning", "metadata_error"}:
            self.statusBar().showMessage(event.get("message", "Ошибка обработки"))
            self.history_summary.setText(event.get("message", "Ошибка обработки"))
        elif kind == "environment":
            self.environment_result.setPlainText(event["text"])
        elif kind == "tools_error":
            self.analysis_status.setText("Проверка инструментов: " + event["text"])
        elif kind == "tools_ready":
            self.statusBar().showMessage(event["text"])
        elif kind == "task_skipped":
            self.statusBar().showMessage(f"Пропущено: {event.get('media_id', '')} · {event.get('reason', '')}")
        elif kind == "queue_error":
            self.statusBar().showMessage(event.get("error", "Ошибка очереди"))

    def poll(self):
        for event in self.queue.poll_events():
            self.handle_backend_event(event)
        self.refresh_queue()

    def closeEvent(self, event):
        self.closing = True
        self.timer.stop()
        self.metadata_cancel.set()
        if self.thumbnail_reply:
            self.thumbnail_reply.abort()
        self.queue.shutdown(timeout=2)
        event.accept()
