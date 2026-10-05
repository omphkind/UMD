"""Media, collection and rename controllers extending the existing desktop shell."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
                              QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox)

from app.core.errors import UmdError
from app.ui.formats import quality_choices, subtitle_choices, video_formats, audio_formats


MEDIA_LABELS = {"video": "Видео", "audio": "Аудио", "photo": "Фото", "auto": "По типу объекта",
                "subtitles": "Субтитры", "thumbnail": "Обложка", "metadata": "Только метаданные"}


class MediaExpansionMixin:
    def init_media_state(self):
        self.analysis_requests = {}
        self.analysis_results = {}
        self.analysis_errors = {}
        self.collection_dialog = None
        self.collection_request = None
        self.collection_confirmed = False
        self.rename_options = {}
        self.rename_verified = False
        self._library_tasks = []
        self.rename_preparation = None
        self._rename_generation = 0

    def build_media_controls(self, layout):
        extras = QHBoxLayout()
        self.preserve_metadata = QCheckBox("Сохранить метаданные")
        self.preserve_metadata.setChecked(getattr(self.settings, "metadata_preserve", True))
        self.embed_cover = QCheckBox("Встроить обложку в аудио")
        self.embed_cover.setChecked(getattr(self.settings, "embed_cover", True))
        self.audio_bitrate = QComboBox()
        for text, value in (("Лучший битрейт", "best"), ("128 kbps", "128"), ("192 kbps", "192"), ("256 kbps", "256"), ("320 kbps", "320")):
            self.audio_bitrate.addItem(text, value)
        self.audio_bitrate.setCurrentIndex(max(0, self.audio_bitrate.findData(getattr(self.settings, "audio_bitrate", "best"))))
        extras.addWidget(self.preserve_metadata)
        extras.addWidget(self.embed_cover)
        extras.addWidget(self.audio_bitrate)
        layout.addLayout(extras)
        organize = QHBoxLayout()
        self.rename_before = QCheckBox("Переименовать перед загрузкой")
        self.rename_before.setChecked(getattr(self.settings, "rename_before_download", False) or getattr(self.settings, "folder_organization", False))
        self.rename_before.toggled.connect(lambda: setattr(self, "rename_verified", False))
        organize.addWidget(self.rename_before)
        self.rename_preset = QComboBox()
        self.refresh_rename_presets()
        self.rename_preset.currentIndexChanged.connect(self.choose_rename_preset)
        organize.addWidget(self.rename_preset)
        self.rename_button = QPushButton("Rename & Organize…")
        self.rename_button.clicked.connect(self.rename_downloads)
        organize.addWidget(self.rename_button)
        self.collection_button = QPushButton("Выбрать элементы…")
        self.collection_button.clicked.connect(self.select_collection)
        self.collection_button.setVisible(False)
        organize.addWidget(self.collection_button)
        layout.addLayout(organize)
        self.collection_label = QLabel()
        self.collection_label.setWordWrap(True)
        layout.addWidget(self.collection_label)
        self.container.currentIndexChanged.connect(self.refresh_audio_options)
        self.url.textChanged.connect(self.cancel_rename_if_input_changed)
        self.refresh_audio_options()

    def refresh_rename_presets(self):
        presets = self.read_rename_presets()
        previous = self.rename_preset.currentData() or getattr(self.settings, "rename_default_preset", "Video")
        self.rename_preset.blockSignals(True)
        self.rename_preset.clear()
        for name in presets:
            self.rename_preset.addItem(name, name)
        index = self.rename_preset.findData(previous)
        self.rename_preset.setCurrentIndex(max(0, index))
        self.rename_preset.blockSignals(False)

    def choose_rename_preset(self):
        options = self.read_rename_presets().get(self.rename_preset.currentData(), {})
        self.rename_options = dict(options)
        template = getattr(self.settings, "rename_template", "{title}")
        if template != "{title}":
            self.rename_options["template"] = template
        self.rename_options.update(start=getattr(self.settings, "rename_number_start", 1), step=getattr(self.settings, "rename_number_step", 1),
                                   padding=getattr(self.settings, "rename_number_padding", 0), collision_policy=getattr(self.settings, "collision_policy", "ask"))
        if getattr(self.settings, "folder_organization", False):
            self.rename_options.update(organization_enabled=True, folder_template=self.settings.folder_template)
        self.rename_verified = False

    def read_rename_presets(self):
        """Preserve an unreadable user file while keeping the desktop usable."""
        from app.rename.presets import PresetStore
        from PySide6.QtCore import QTimer
        try:
            presets = PresetStore(self.store.data_dir / "UMD_RENAME_PRESETS.json").list()
            self.rename_preset_warning = ""
            return presets
        except (OSError, ValueError, UmdError) as error:
            warning = "Не удалось прочитать пресеты. Файл сохранён; используем стандартные шаблоны. " + str(error)
            self.rename_preset_warning = warning
            self.statusBar().showMessage(warning)
            # During construction, the normal welcome text is set afterwards.
            QTimer.singleShot(0, self, lambda: self.statusBar().showMessage(warning))
            try:
                return PresetStore().list()
            except (OSError, ValueError, UmdError):
                return {}

    def _has_ffmpeg(self):
        from app.downloader.ytdlp import YtDlp
        try:
            return bool(YtDlp(self.settings)._executable(self.settings.ffmpeg_path, "ffmpeg", required=False))
        except UmdError:
            return False

    def refresh_audio_options(self):
        if not hasattr(self, "embed_cover"):
            return
        audio = self.kind.currentData() == "audio"
        self.embed_cover.setVisible(audio)
        self.embed_cover.setEnabled(audio and bool((self.analysis or {}).get("thumbnail") or (self.analysis or {}).get("entries")) and self._has_ffmpeg())
        self.audio_bitrate.setVisible(audio)
        self.audio_bitrate.setEnabled(audio and self.container.currentData() not in {"original", "wav", "flac"} and self._has_ffmpeg())
        self.rename_verified = False

    def refresh_media_choices(self):
        analysis = self.analysis or {}
        entries = analysis.get("entries") or []
        collection = analysis.get("media_type") in {"playlist", "gallery", "collection", "album", "profile"} or bool(entries)
        kind = self.kind.currentData() or self.settings.download_type
        same_kind = getattr(self, "_choice_kind", None) == kind
        default_quality = getattr(self.settings, f"default_{kind}_quality", self.settings.download_quality)
        previous = (self.quality.currentData() if same_kind else None) or default_quality
        self.quality.clear()
        for title, value in quality_choices(analysis, kind, self.advanced.isChecked()):
            self.quality.addItem(title, value)
        if collection and not self.quality.count() and kind in {"video", "audio", "auto"}:
            self.quality.addItem("Лучшее для каждого объекта", "best")
        if kind == "photo" and not self.quality.count() and collection:
            self.quality.addItem("Исходное фото каждого объекта", "original")
        index = self.quality.findData(previous)
        if index >= 0:
            self.quality.setCurrentIndex(index)
        self.quality.setEnabled(kind in {"video", "audio", "photo"} and bool(self.quality.count()))
        old_container = self.container.currentData() if same_kind else None
        self._choice_kind = kind
        self.container.clear()
        if kind == "audio":
            containers = ["original", "mp3", "m4a", "opus", "wav", "flac"] if self._has_ffmpeg() else ["original"]
            default = getattr(self.settings, "default_audio_format", "mp3")
        elif kind == "photo":
            containers, default = ["original"], "original"
        elif kind == "auto":
            containers, default = ["original"], "original"
        else:
            containers = ["mp4", "mkv", "webm", "mov", "original"] if self._has_ffmpeg() else ["original"]
            default = getattr(self.settings, "default_video_format", "mp4")
        for value in containers:
            self.container.addItem("Исходный формат" if value == "original" else value.upper(), value)
        index = self.container.findData(old_container if old_container in containers else default)
        self.container.setCurrentIndex(max(0, index))
        self.container.setEnabled(kind in {"video", "audio", "photo"})
        self.audio.setVisible(kind == "video")
        self.option_labels[self.audio].setVisible(kind == "video")
        self.audio.setEnabled(kind == "video")
        self.subtitles.clear()
        for title, value in subtitle_choices(analysis):
            self.subtitles.addItem(title, value)
        if collection and kind in {"video", "audio"} and self.subtitles.findData("all") < 0:
            self.subtitles.addItem("Все доступные для каждого объекта", "all")
        index = self.subtitles.findData(self.settings.download_subtitles)
        self.subtitles.setCurrentIndex(max(0, index))
        self.subtitles.setVisible(kind in {"video", "audio", "subtitles"})
        self.subtitle_format.setVisible(kind in {"video", "audio", "subtitles"})
        self.option_labels[self.subtitles].setVisible(kind in {"video", "audio", "subtitles"})
        self.option_labels[self.subtitle_format].setVisible(kind in {"video", "audio", "subtitles"})
        self.refresh_subtitle_format()
        self.save_thumbnail.setEnabled(bool(analysis.get("thumbnail")) or collection)
        self.save_thumbnail.setVisible(kind in {"video", "audio"})
        self.save_chapters.setEnabled(bool(analysis.get("chapters")) or collection)
        self.save_chapters.setVisible(kind in {"video", "audio"})
        self.refresh_audio_options()
        status = analysis.get("capability_status") or analysis.get("support_status")
        available = bool(self.analysis) and (kind == "metadata" or status not in {"unsupported", "authentication_required", "metadata_only"} and (
            collection or kind == "video" and video_formats(analysis) or kind == "audio" and audio_formats(analysis)
            or kind == "photo" and analysis.get("photo_formats") or kind == "subtitles" and self.subtitles.count() > 1
            or kind == "thumbnail" and analysis.get("thumbnail")))
        if collection and not self.collection_confirmed:
            available = False
        if hasattr(self, "download_button"):
            self.download_button.setEnabled(bool(available))

    def media_analysis_shown(self, analysis):
        entries = analysis.get("entries") or []
        self.collection_confirmed = not bool(entries)
        self.rename_verified = False
        kinds = {item.get("media_type") for item in entries if item.get("media_type") in {"video", "audio", "photo"}}
        native = analysis.get("media_type")
        preferred = "auto" if len(kinds) > 1 else next(iter(kinds), native)
        capabilities = set(analysis.get("capabilities") or [])
        choices = []
        if entries:
            choices.append("auto")
            choices += [kind for kind in ("video", "audio", "photo") if kind in kinds or kind in capabilities]
            if not kinds and not choices[1:]:
                choices += ["video", "audio"]
        else:
            if video_formats(analysis):
                choices.append("video")
            if audio_formats(analysis):
                choices.append("audio")
            if analysis.get("photo_formats") or native == "photo":
                choices.append("photo")
            if analysis.get("subtitles") or analysis.get("automatic_captions"):
                choices.append("subtitles")
            if analysis.get("thumbnail"):
                choices.append("thumbnail")
        choices.append("metadata")
        self.kind.blockSignals(True)
        self.kind.clear()
        for kind in choices:
            self.kind.addItem(MEDIA_LABELS[kind], kind)
        self.kind.setCurrentIndex(max(0, self.kind.findData(preferred)))
        self.kind.blockSignals(False)
        self.collection_button.setVisible(bool(entries))
        self.collection_label.setText(f"Найдено на странице: {len(entries)}. Выберите нужные элементы перед загрузкой." if entries else "")
        reason = analysis.get("capability_reason") or analysis.get("reason") or analysis.get("support_reason") or ""
        status = analysis.get("capability_status") or analysis.get("support_status")
        if status:
            text = {"supported": "Загрузка доступна", "supported_with_limitations": "Поддержка с ограничениями",
                    "authentication_required": "Требуется авторизация", "metadata_only": "Доступны только метаданные",
                    "unsupported": "Источник пока не поддерживается"}.get(status, status)
            self.analysis_status.setText(text + (" · " + str(reason) if reason else ""))
        self.refresh_media_choices()

    def analyze_urls(self):
        self.cancel_rename_preparation(notify=False)
        urls = self.input_urls()
        if not urls:
            self.statusBar().showMessage("Вставьте хотя бы одну ссылку.")
            return
        self.queue.cancel_analysis()
        self.analysis = None
        self.analysis_requests.clear()
        self.analysis_results.clear()
        self.analysis_errors.clear()
        self.analyzed_urls = list(urls)
        self.analyze_button.setEnabled(False)
        self.download_button.setEnabled(False)
        self.analysis_status.setText(f"Анализируем ссылок: {len(urls)}…")
        for index, url in enumerate(urls):
            try:
                request = self.queue.analyze(url)
                self.analysis_requests[request] = (index, url)
                if index == 0:
                    self.analysis_request = request
            except (UmdError, ValueError, OSError) as error:
                self.analysis_errors[index] = str(error)
        if not self.analysis_requests:
            self.analysis_error("; ".join(self.analysis_errors.values()))

    def handle_media_event(self, event):
        request = event.get("request_id")
        kind = event.get("type")
        preparation = self.rename_preparation
        if preparation and request in preparation["pending"] and kind in {"analysis_ready", "analysis_failed", "analysis_cancelled"}:
            self.handle_rename_preparation(event)
            return True
        if request and request == self.collection_request and self.collection_dialog:
            if kind == "analysis_ready":
                self.collection_dialog.set_page(event["analysis"])
                self.collection_request = None
            elif kind in {"analysis_failed", "analysis_cancelled"}:
                self.collection_dialog.page_error(event.get("error") or "Анализ отменён")
                self.collection_request = None
            return True
        if request not in self.analysis_requests or kind not in {"analysis_ready", "analysis_failed", "analysis_cancelled"}:
            return False
        index, url = self.analysis_requests.pop(request)
        if self.input_urls() != self.analyzed_urls:
            return True
        if kind == "analysis_ready":
            self.analysis_results[index] = event["analysis"]
        else:
            self.analysis_errors[index] = event.get("error") or "Анализ отменён"
        self.analysis_status.setText(f"Проанализировано: {len(self.analysis_results) + len(self.analysis_errors)} / {len(self.analyzed_urls)}")
        if self.analysis_requests:
            return True
        self.analyze_button.setEnabled(True)
        if not self.analysis_results:
            self.analysis_error("; ".join(self.analysis_errors.values()))
            return True
        results = [self.analysis_results[key] for key in sorted(self.analysis_results)]
        if len(results) == 1:
            analysis = results[0]
        else:
            entries = [item for result in results for item in (result.get("entries") or [result])]
            analysis = {"id": "multi-url", "source": "mixed", "media_type": "playlist", "collection_type": "collection",
                        "title": "Несколько ссылок", "entries": entries, "collections": results,
                        "capabilities": sorted({cap for result in results for cap in result.get("capabilities", [])}),
                        "capability_status": "supported_with_limitations" if self.analysis_errors else "supported", "page": 1}
        self.show_analysis(analysis)
        if self.analysis_errors:
            self.analysis_status.setText(self.analysis_status.text() + f" · Ошибок ссылок: {len(self.analysis_errors)}; доступные результаты сохранены.")
        return True

    def select_collection(self):
        if not self.analysis or not self.analysis.get("entries"):
            return
        from app.ui.gallery_view import GallerySelectionDialog
        dialog = GallerySelectionDialog(self.analysis, self, self.queue_thumbnails, self.fetch_queue_thumbnail,
                                        lazy_loading=getattr(self.settings, "lazy_loading", True))
        self.collection_dialog = dialog
        def request_page(page):
            url = dialog.analysis.get("input_url") or dialog.analysis.get("url")
            try:
                self.collection_request = self.queue.analyze(url, page=page, page_size=getattr(self.settings, "gallery_page_size", 80))
            except (UmdError, ValueError) as error:
                dialog.page_error(str(error))
        dialog.page_requested.connect(request_page)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if self.collection_request:
            self.queue.cancel_analysis(self.collection_request)
            self.collection_request = None
        self.collection_dialog = None
        if accepted:
            self.analysis = {**self.analysis, "entries": dialog.selected_entries}
            self.collection_confirmed = True
            self.collection_label.setText(f"Выбрано элементов: {len(dialog.selected_entries)}")
            self.rename_verified = False
            self.refresh_media_choices()

    def rename_items(self):
        from app.tasks.queue import original_audio_output_extension
        items = deepcopy(self.analysis.get("entries") or [self.analysis]) if self.analysis else []
        container = self.container.currentData()
        for item in items:
            native = item.get("media_type")
            chosen = self.kind.currentData()
            if chosen == "auto":
                container_for_item = "original" if item.get("backend") in {"gallery-dl", "direct-http"} else getattr(self.settings, "default_audio_format", "mp3") if native == "audio" else getattr(self.settings, "default_video_format", "mp4") if native == "video" else "original"
            else:
                container_for_item = container
            formats = item.get("photo_formats") or item.get("formats") or []
            kind = native if chosen == "auto" else chosen
            if kind in {"metadata", "subtitles", "thumbnail"}:
                item["ext"] = "json" if kind == "metadata" else self.subtitle_format.currentData() if kind == "subtitles" else "jpg"
                continue
            if (container_for_item == "original" and (kind == "audio" or self.audio.currentData() == "audio_only")
                    and item.get("backend") not in {"gallery-dl", "direct-http"}):
                quality = getattr(self.settings, "default_audio_quality", "best") if chosen == "auto" else self.quality.currentData() or "best"
                extension = original_audio_output_extension(item, quality)
                if not extension:
                    from app.core.errors import ConfigurationError
                    raise ConfigurationError("Оригинальный аудиокодек не определён. Выберите конкретный формат перед переименованием.")
                item["ext"] = extension
            else:
                item["ext"] = container_for_item if container_for_item != "original" else item.get("ext") or (formats[-1].get("ext") if formats else None) or Path(item.get("original_filename", "")).suffix.lstrip(".") or "bin"
        return items

    def rename_downloads(self):
        if self.rename_preparation:
            self.cancel_rename_preparation()
            return False
        if not self.analysis:
            self.statusBar().showMessage("Сначала выполните анализ ссылок.")
            return False
        if self.analysis.get("entries") and not self.collection_confirmed:
            self.select_collection()
            if not self.collection_confirmed:
                return False
        items = self.analysis.get("entries") or [self.analysis]
        if self.kind.currentData() in {"auto", "video", "audio", "photo"} and any(self.rename_item_needs_analysis(item) for item in items):
            self.prepare_selected_rename_items(items)
            return False
        from app.ui.rename_dialog import RenameDialog
        if not self.rename_options:
            self.choose_rename_preset()
        try:
            dialog = RenameDialog(self.rename_items(), self.output.text(), self, local_files=False,
                                  settings={"rename": self.rename_options}, presets=self.store.data_dir / "UMD_RENAME_PRESETS.json")
        except (UmdError, ValueError, OSError) as error:
            self.statusBar().showMessage(str(error))
            return False
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        self.rename_options = dict(dialog.options)
        self.output.setText(dialog.output_dir)
        self.rename_before.setChecked(True)
        self.rename_verified = True
        self.refresh_rename_presets()
        self.statusBar().showMessage("План переименования применён к новым загрузкам.")
        return True

    @staticmethod
    def rename_item_needs_analysis(item):
        return item.get("backend") not in {"gallery-dl", "direct-http"} and (item.get("needs_analysis") or not item.get("formats"))

    def prepare_selected_rename_items(self, items):
        """Resolve selected flat items only, with a bounded in-flight window."""
        self._rename_generation += 1
        copies = deepcopy(items)
        self.rename_preparation = {"generation": self._rename_generation, "items": copies,
                                   "indexes": [index for index, item in enumerate(copies) if self.rename_item_needs_analysis(item)],
                                   "next": 0, "pending": {}, "finished": 0,
                                   "input_urls": tuple(self.input_urls()), "analysis": self.analysis}
        self.rename_verified = False
        self.rename_button.setText("Отменить подготовку имён")
        self.collection_button.setEnabled(False)
        self.download_button.setEnabled(False)
        self.dispatch_rename_analysis()

    def dispatch_rename_analysis(self):
        state = self.rename_preparation
        if not state:
            return
        capacity = max(1, min(8, getattr(self.settings, "gallery_max_concurrency", 3)))
        while len(state["pending"]) < capacity and state["next"] < len(state["indexes"]):
            index = state["indexes"][state["next"]]
            item = state["items"][index]
            try:
                url = item.get("url")
                if not url:
                    raise ValueError("У выбранного объекта отсутствует ссылка для анализа.")
                page_size = getattr(self.settings, "gallery_page_size", 80)
                playlist_index = item.get("playlist_index")
                page = (playlist_index - 1) // page_size + 1 if item.get("playlist_url") == url and isinstance(playlist_index, int) and playlist_index > 0 else 1
                request = self.queue.analyze(url, page=page, page_size=page_size)
            except (UmdError, ValueError, OSError) as error:
                self.fail_rename_preparation(str(error))
                return
            state["pending"][request] = index
            state["next"] += 1
        self.statusBar().showMessage(f"Уточняем метаданные выбранных объектов: {state['finished']} / {len(state['indexes'])}. Можно отменить подготовку.")
        if not state["pending"] and state["next"] == len(state["indexes"]):
            from PySide6.QtCore import QTimer
            generation = state["generation"]
            QTimer.singleShot(0, self, lambda: self.finish_rename_preparation(generation))

    def handle_rename_preparation(self, event):
        state = self.rename_preparation
        if not state:
            return
        if tuple(self.input_urls()) != state["input_urls"] or self.analysis is not state["analysis"] or getattr(self, "closing", False):
            self.cancel_rename_preparation(notify=False)
            return
        index = state["pending"].pop(event["request_id"])
        previous = state["items"][index]
        if event["type"] != "analysis_ready":
            self.fail_rename_preparation(event.get("error") or "Анализ выбранного объекта отменён.")
            return
        actual = deepcopy(event["analysis"])
        if actual.get("entries"):
            expected_id = str(previous.get("id") or previous.get("media_id") or "")
            actual = next((entry for entry in actual["entries"] if str(entry.get("id") or entry.get("media_id") or "") == expected_id and
                           str(entry.get("source") or actual.get("source") or "") == str(previous.get("source") or actual.get("source") or "")), None)
            if actual is None:
                self.fail_rename_preparation("Выбранный объект больше не найден в коллекции. Повторите анализ.")
                return
            actual = deepcopy(actual)
        if not actual.get("formats") or actual.get("capability_status") in {"unsupported", "authentication_required", "metadata_only"}:
            self.fail_rename_preparation(actual.get("capability_reason") or actual.get("reason") or "У выбранного объекта нет доступного формата. Имя нельзя подтвердить до анализа.")
            return
        for field in ("collection", "collection_id", "collection_name", "collection_type", "collection_index",
                      "gallery_name", "gallery_index", "playlist_index", "playlist_url", "source_context", "media_context"):
            if field in previous:
                actual[field] = previous[field]
        actual["needs_analysis"] = False
        state["items"][index] = actual
        state["finished"] += 1
        self.dispatch_rename_analysis()

    def finish_rename_preparation(self, generation):
        state = self.rename_preparation
        if not state or state["generation"] != generation:
            return
        if tuple(self.input_urls()) != state["input_urls"] or self.analysis is not state["analysis"] or getattr(self, "closing", False):
            self.cancel_rename_preparation(notify=False)
            return
        if self.analysis.get("entries"):
            self.analysis = {**self.analysis, "entries": state["items"]}
        else:
            self.analysis = state["items"][0]
        self.cancel_rename_preparation(notify=False)
        if self.rename_downloads():
            self.statusBar().showMessage("Имена выбранных объектов подтверждены. Нажмите «Скачать» для запуска очереди.")

    def fail_rename_preparation(self, message):
        state = self.rename_preparation
        # Keep successfully obtained metadata for a subsequent retry, without
        # silently dropping failed selected entries or adding anything to queue.
        if state and self.analysis is state["analysis"] and self.analysis.get("entries"):
            self.analysis = {**self.analysis, "entries": state["items"]}
        self.cancel_rename_preparation(notify=False)
        self.statusBar().showMessage("Не удалось подготовить имена: " + str(message))

    def cancel_rename_if_input_changed(self):
        state = self.rename_preparation
        if state and tuple(self.input_urls()) != state["input_urls"]:
            self.cancel_rename_preparation(notify=False)

    def cancel_rename_preparation(self, notify=True):
        state = self.rename_preparation
        if not state:
            return
        self.rename_preparation = None
        self._rename_generation += 1
        for request in state["pending"]:
            self.queue.cancel_analysis(request)
        self.rename_button.setText("Rename & Organize…")
        self.collection_button.setEnabled(True)
        self.refresh_media_choices()
        if notify:
            self.statusBar().showMessage("Подготовка имён отменена. Файлы не изменены и загрузки не запущены.")

    def enqueue_selection(self):
        if not self.analysis:
            return
        if self.analysis.get("entries") and not self.collection_confirmed:
            self.select_collection()
            if not self.collection_confirmed:
                return
        if self.rename_before.isChecked() and not self.rename_verified and not self.rename_downloads():
            return
        options = {"media_type": self.kind.currentData(), "quality": self.quality.currentData() or "best",
                   "container": self.container.currentData(), "audio": self.audio.currentData(),
                   "subtitles": self.subtitles.currentData(), "subtitle_format": self.subtitle_format.currentData(),
                   "thumbnail": self.save_thumbnail.isChecked(), "chapters": self.save_chapters.isChecked(),
                   "output_path": self.output.text().strip(), "metadata_preserve": self.preserve_metadata.isChecked(),
                   "embed_cover": self.embed_cover.isChecked(), "audio_bitrate": self.audio_bitrate.currentData(),
                   "rename": self.rename_options if self.rename_before.isChecked() else {},
                   "collision_policy": self.rename_options.get("collision_policy", getattr(self.settings, "collision_policy", "ask"))}
        try:
            if options["media_type"] == "metadata" and self.analysis.get("source") == "youtube":
                self.run_metadata("new", self.input_urls())
                self.switch_page(3)
                return
            tasks = self.queue.enqueue(self.analysis, options)
            self.statusBar().showMessage(f"Добавлено задач: {len(tasks)}")
            self.switch_page(1)
            self.refresh_queue(force=True)
        except (UmdError, ValueError, OSError) as error:
            self.statusBar().showMessage(str(error))

    def add_media_settings(self, form):
        for name, title in (("gallery_dl_path", "Путь к gallery-dl"), ("cookie_file", "Файл cookies (Netscape)")):
            field = QLineEdit(getattr(self.settings, name, ""))
            row = QHBoxLayout()
            row.addWidget(field)
            choose = QPushButton("Обзор…")
            def browse(checked=False, widget=field, setting=name):
                path, _ = QFileDialog.getOpenFileName(self, "Выберите файл", widget.text(), "Все файлы (*)")
                if path:
                    widget.setText(path)
            choose.clicked.connect(browse)
            row.addWidget(choose)
            form.addRow(title, row)
            self.setting_fields[name] = field
        for name, title, choices in (
            ("auth_mode", "Авторизация", [("Анонимно", "anonymous"), ("Предоставленный файл cookies", "cookies")]),
            ("default_video_format", "Видео по умолчанию", [(x.upper(), x) for x in ("mp4", "mkv", "webm", "mov", "original")]),
            ("default_audio_format", "Аудио по умолчанию", [(x.upper(), x) for x in ("mp3", "m4a", "opus", "flac", "wav", "original")]),
            ("default_photo_quality", "Качество фото", [("Оригинал", "original"), ("Лучшее доступное", "best")]),
            ("collision_policy", "Совпадение имён", [("Спросить", "ask"), ("Пропустить", "skip"), ("Перезаписать", "overwrite"), ("Добавить номер", "append_number")]),
        ):
            widget = QComboBox()
            for text, value in choices:
                widget.addItem(text, value)
            widget.setCurrentIndex(max(0, widget.findData(getattr(self.settings, name, choices[0][1]))))
            self.setting_fields[name] = widget
            form.addRow(title, widget)
        for name, title, minimum, maximum in (
            ("simultaneous_downloads", "Одновременных загрузок", 1, 8), ("gallery_page_size", "Элементов на странице", 10, 200),
            ("gallery_max_concurrency", "Одновременных анализов", 1, 8), ("rename_number_start", "Нумерация: начало", 1, 999999),
            ("rename_number_step", "Нумерация: шаг", 1, 9999), ("rename_number_padding", "Нумерация: разрядов", 0, 12),
        ):
            widget = QSpinBox()
            widget.setRange(minimum, maximum)
            widget.setValue(getattr(self.settings, name, minimum))
            self.setting_fields[name] = widget
            form.addRow(title, widget)
        for name, title in (("thumbnail_cache", "Кэш миниатюр"), ("lazy_loading", "Ленивая загрузка миниатюр"),
                            ("folder_organization", "Раскладывать файлы по папкам")):
            widget = QCheckBox(title)
            widget.setChecked(getattr(self.settings, name, True))
            self.setting_fields[name] = widget
            form.addRow("", widget)
        for name, title in (("folder_template", "Шаблон папок"), ("rename_template", "Шаблон имени")):
            widget = QLineEdit(getattr(self.settings, name, ""))
            self.setting_fields[name] = widget
            form.addRow(title, widget)

    def rename_completed(self, from_queue=False, local_files=False):
        tasks = self.queue.snapshot()
        if local_files:
            files, _ = QFileDialog.getOpenFileNames(self, "Файлы для Rename & Organize", self.output.text(), "Все файлы (*)")
            items = [{"path": path} for path in files]
        else:
            table = self.queue_table if from_queue else self.library
            identifiers = {table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole) for index in table.selectionModel().selectedRows()}
            items = [{**task.get("analysis", {}), "path": file} for task in tasks if task["id"] in identifiers and task["status"] == "Completed" for file in task.get("files", []) if Path(file).is_file()]
        if not items:
            self.statusBar().showMessage("Выберите готовые файлы для переименования.")
            return
        from app.ui.rename_dialog import RenameDialog
        dialog = RenameDialog(items, self.output.text(), self, local_files=True,
                              presets=self.store.data_dir / "UMD_RENAME_PRESETS.json")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.queue.update_file_paths(dialog.mapping)
            self.refresh_queue(force=True)
            self.statusBar().showMessage(f"Переименовано файлов: {len(dialog.mapping)}")

    def render_download_history(self):
        if not hasattr(self, "download_history"):
            return
        kind = self.history_filter.currentData()
        tasks = self.queue.snapshot(compact=True)
        lines = []
        for task in tasks:
            media_type = task.get("media_type") or task.get("options", {}).get("media_type", "video")
            if kind in {"photo", "audio", "video"} and media_type != kind or kind in {"Failed", "Completed"} and task["status"] != kind:
                continue
            lines.append(f"{MEDIA_LABELS.get(media_type, media_type)} · {task['status']} · {task.get('title') or task['url']}\n" + "\n".join(task.get("files", [])))
        self.download_history.setPlainText("\n\n".join(lines) or "Загрузок с выбранным фильтром пока нет.")
