"""Paged collection selection; only visible thumbnails are requested."""
from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal, QTimer, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                              QComboBox, QTableView, QHeaderView, QSpinBox)

from app.tasks.selection import SelectionManager


def identity(item):
    return f"{item.get('source', '')}:{item.get('id') or item.get('media_id') or item.get('url')}"


class CollectionModel(QAbstractTableModel):
    HEADERS = ["Выбор", "Предпросмотр", "№", "Название", "Тип", "Автор", "Размер"]
    selection_changed = Signal()

    def __init__(self, items=(), thumbnail_cache=None, request_thumbnail=None, parent=None):
        super().__init__(parent)
        self.items = list(items)
        self.visible = list(range(len(self.items)))
        self.selection = SelectionManager(self.items)
        self.thumbnail_cache = thumbnail_cache if thumbnail_cache is not None else {}
        self.request_thumbnail = request_thumbnail

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.visible)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self.items[self.visible[index.row()]]
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            return Qt.CheckState.Checked if self.selection.is_selected(item) else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DecorationRole and index.column() == 1:
            url = item.get("thumbnail")
            if url and url not in self.thumbnail_cache and self.request_thumbnail:
                self.request_thumbnail(url)
            return self.thumbnail_cache.get(url, QIcon())
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(item.get("original_filename") or item.get("url") or "")
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        size = " × ".join(str(item[k]) for k in ("width", "height") if item.get(k))
        values = ["", "", item.get("collection_index") or item.get("playlist_index") or self.visible[index.row()] + 1,
                  item.get("title") or item.get("original_filename") or item.get("id"),
                  {"video": "Видео", "audio": "Аудио", "photo": "Фото"}.get(item.get("media_type"), item.get("media_type", "Медиа")),
                  item.get("author", ""), size or "—"]
        return str(values[index.column()] or "")

    def flags(self, index):
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return flags | Qt.ItemFlag.ItemIsUserCheckable if index.column() == 0 else flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0 and index.isValid():
            item = self.items[self.visible[index.row()]]
            checked = value == Qt.CheckState.Checked or value == Qt.CheckState.Checked.value
            (self.selection.select if checked else self.selection.deselect)(item)
            self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
            self.selection_changed.emit()
            return True
        return False

    def filter(self, media_type):
        self.beginResetModel()
        self.visible = [i for i, item in enumerate(self.items) if not media_type or item.get("media_type") == media_type]
        self.endResetModel()

    def choose(self, operation):
        for row in self.visible:
            item = self.items[row]
            checked = operation == "all" or operation == "invert" and not self.selection.is_selected(item)
            (self.selection.select if checked else self.selection.deselect)(item)
        if self.rowCount():
            self.dataChanged.emit(self.index(0, 0), self.index(self.rowCount() - 1, 0), [Qt.ItemDataRole.CheckStateRole])
        self.selection_changed.emit()


class GallerySelectionDialog(QDialog):
    page_requested = Signal(int)

    def __init__(self, analysis, parent=None, thumbnail_cache=None, request_thumbnail=None, *, lazy_loading=True):
        super().__init__(parent)
        self.lazy_loading = lazy_loading
        self.setWindowTitle("UMD — Выбор элементов коллекции")
        self.resize(1000, 640)
        self.selected_entries = []
        self._selected = {}
        self.analysis = analysis
        self.thumbnail_cache = thumbnail_cache if thumbnail_cache is not None else {}
        self.request_thumbnail = request_thumbnail
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        controls = QHBoxLayout()
        for title, operation in (("Выбрать страницу", "all"), ("Снять выбор", "none"), ("Инвертировать", "invert")):
            widget = QPushButton(title)
            widget.clicked.connect(lambda checked=False, op=operation: self.model.choose(op))
            controls.addWidget(widget)
        self.filter_box = QComboBox()
        for text, value in (("Все типы", ""), ("Фото", "photo"), ("Видео", "video"), ("Аудио", "audio")):
            self.filter_box.addItem(text, value)
        self.filter_box.currentIndexChanged.connect(lambda: self.model.filter(self.filter_box.currentData()))
        controls.addWidget(self.filter_box)
        layout.addLayout(controls)
        if analysis.get("collections"):
            sources = QComboBox()
            sources.addItem("Все результаты анализа", analysis)
            for item in analysis["collections"]:
                if item.get("entries"):
                    sources.addItem(str(item.get("title") or item.get("source") or item.get("input_url")), item)
            sources.currentIndexChanged.connect(lambda: self.set_page(sources.currentData()))
            layout.addWidget(sources)
        self.table = QTableView()
        self.table.setIconSize(QSize(72, 44))
        self.table.verticalHeader().setDefaultSectionSize(54)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)
        ranges = QHBoxLayout()
        ranges.addWidget(QLabel("Выбрать строки страницы"))
        self.range_start = QSpinBox()
        self.range_end = QSpinBox()
        for field in (self.range_start, self.range_end):
            field.setRange(1, 100000)
            ranges.addWidget(field)
        choose_range = QPushButton("Выбрать диапазон")
        choose_range.clicked.connect(self.select_range)
        ranges.addWidget(choose_range)
        ranges.addStretch()
        layout.addLayout(ranges)
        pages = QHBoxLayout()
        self.previous = QPushButton("Предыдущая страница")
        self.previous.clicked.connect(lambda: self.request_page(max(1, self.analysis.get("page", 1) - 1)))
        self.next = QPushButton("Следующая страница")
        self.next.clicked.connect(lambda: self.request_page(self.analysis.get("next_page") or self.analysis.get("page", 1) + 1))
        pages.addWidget(self.previous)
        pages.addWidget(self.next)
        pages.addStretch()
        self.accept_button = QPushButton("Использовать выбранные")
        self.accept_button.setObjectName("primary")
        self.accept_button.clicked.connect(self.accept_selection)
        pages.addWidget(self.accept_button)
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        pages.addWidget(cancel)
        layout.addLayout(pages)
        self.set_page(analysis)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_thumbnails)
        self.timer.start(750)

    def remember_selection(self):
        for item in self.model.items:
            key = identity(item)
            if self.model.selection.is_selected(item):
                self._selected[key] = item
            else:
                self._selected.pop(key, None)

    def set_page(self, analysis):
        if hasattr(self, "model"):
            self.remember_selection()
        self.analysis = analysis
        self.model = CollectionModel(analysis.get("entries") or [], self.thumbnail_cache, self.request_thumbnail, self)
        for item in self.model.items:
            if identity(item) in self._selected:
                self.model.selection.select(item)
        self.model.selection_changed.connect(self.update_summary)
        self.table.setModel(self.model)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 60)
        self.table.setColumnWidth(1, 86)
        self.table.setColumnWidth(2, 45)
        self.model.filter(self.filter_box.currentData())
        self.range_end.setValue(max(1, len(self.model.items)))
        self.previous.setEnabled(analysis.get("page", 1) > 1)
        self.next.setEnabled(bool(analysis.get("has_more")))
        self.accept_button.setEnabled(True)
        self.update_summary()

    def update_summary(self):
        self.remember_selection()
        warning = self.analysis.get("enumeration_error") or self.analysis.get("reason") or ""
        self.summary.setText(f"{self.analysis.get('title') or 'Коллекция'} · Страница {self.analysis.get('page', 1)} · "
                             f"На странице: {len(self.model.items)} · Выбрано: {len(self._selected)}"
                             + ("\n" + str(warning) if warning else ""))

    def request_page(self, page):
        self.remember_selection()
        self.previous.setEnabled(False)
        self.next.setEnabled(False)
        self.accept_button.setEnabled(False)
        self.summary.setText("Получаем следующую страницу…")
        self.page_requested.emit(page)

    def page_error(self, message):
        self.summary.setText("Не удалось получить страницу: " + message)
        self.previous.setEnabled(self.analysis.get("page", 1) > 1)
        self.next.setEnabled(bool(self.analysis.get("has_more")))
        self.accept_button.setEnabled(True)

    def select_range(self):
        start, end = self.range_start.value() - 1, self.range_end.value() - 1
        for row in range(max(0, start), min(len(self.model.visible), end + 1)):
            self.model.selection.select(self.model.items[self.model.visible[row]])
        self.model.selection_changed.emit()
        if self.model.rowCount():
            self.model.dataChanged.emit(self.model.index(0, 0), self.model.index(self.model.rowCount() - 1, 0))

    def refresh_thumbnails(self):
        if not self.lazy_loading and self.request_thumbnail:
            # Eager mode prefetches only this bounded page, never the gallery.
            for item in self.model.items:
                url = item.get("thumbnail")
                if url and url not in self.thumbnail_cache:
                    self.request_thumbnail(url)
        if self.model.rowCount():
            self.model.dataChanged.emit(self.model.index(0, 1), self.model.index(self.model.rowCount() - 1, 1), [Qt.ItemDataRole.DecorationRole])

    def accept_selection(self):
        self.remember_selection()
        self.selected_entries = sorted(self._selected.values(), key=lambda item: (str(item.get("collection_id", "")), item.get("collection_index") or item.get("playlist_index") or 0))
        if not self.selected_entries:
            self.summary.setText("Выберите хотя бы один элемент.")
            return
        self.accept()
