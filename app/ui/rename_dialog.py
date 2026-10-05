"""Reusable asynchronous pre-download/local-file rename and organize dialog."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QTableView, QTableWidget, QTableWidgetItem, QVBoxLayout)

from app.rename.presets import PresetStore
from app.rename.service import RenameService
from app.core.errors import UmdError


class _Signals(QObject):
    completed = Signal(object, object, object)


class _Worker(QRunnable):
    def __init__(self, generation, callback):
        super().__init__()
        self.generation, self.callback = generation, callback
        self.signals = _Signals()

    def run(self):
        try:
            result, error = self.callback(), None
        except Exception as failure:
            result, error = None, str(failure)
        self.signals.completed.emit(id(self), result, error)


class RenamePreviewModel(QAbstractTableModel):
    HEADERS = ("Исходное имя", "Новое имя / папка", "Статус")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 3

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return row.get("conflict") or row.get("target_path", "")
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == 0:
                return row.get("original_filename", "")
            if index.column() == 1:
                try:
                    return str(Path(row["target_path"]).relative_to(row["output_dir"]))
                except (KeyError, ValueError):
                    return row.get("target_path", "")
            labels = {"ready": "Готово", "unchanged": "Без изменений", "skipped": "Пропуск",
                      "conflict": "Конфликт", "error": "Ошибка"}
            return (labels.get(row.get("status"), "") + (": " + row["conflict"] if row.get("conflict") else ""))
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return super().headerData(section, orientation, role)

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class RenameDialog(QDialog):
    """Accepted pre-download dialog exposes options/plan; local mode also mapping."""
    applied = Signal(object)
    preview_ready = Signal(object)

    def __init__(self, items, output_dir, parent=None, local_files=False, settings=None, presets=None):
        super().__init__(parent)
        self.setWindowTitle("Переименование и организация")
        self.resize(980, 750)
        self.setMinimumSize(780, 600)
        self.setAcceptDrops(bool(local_files))
        self.items = [self._item(item) for item in items]
        self.local_files = bool(local_files)
        self.service = RenameService()
        self.options = {}
        self.plan = []
        self.mapping = {}
        self.preview_pending = False
        self.applying = False
        self._generation = 0
        self._workers = {}
        self._pool = QThreadPool.globalInstance()
        self._preset_store = presets if isinstance(presets, PresetStore) else PresetStore(presets if isinstance(presets, (str, Path)) else None)
        self.preset_warning = ""
        try:
            self._presets = deepcopy(presets) if isinstance(presets, dict) else self._preset_store.load()
        except (ValueError, OSError, UmdError) as error:
            self.preset_warning = "Не удалось прочитать пресеты. Исходный файл сохранён; загружены стандартные шаблоны. " + str(error)
            try:
                self._presets = PresetStore().load()
            except (ValueError, OSError, UmdError):
                self._presets = {"version": 1, "presets": {}}
        if settings is not None and not isinstance(settings, dict):
            settings = asdict(settings) if is_dataclass(settings) else {}
        initial = (settings or {}).get("rename", settings or {}) or {}
        if "template" not in initial:
            initial = {"enabled": True if local_files else (settings or {}).get("rename_before_download", True),
                       "template": (settings or {}).get("rename_template", "{title}"),
                       "start": (settings or {}).get("rename_number_start", (settings or {}).get("rename_start", 1)),
                       "step": (settings or {}).get("rename_number_step", (settings or {}).get("rename_step", 1)),
                       "padding": (settings or {}).get("rename_number_padding", (settings or {}).get("rename_padding", 0)),
                       "collision_policy": (settings or {}).get("collision_policy", "ask"),
                       "organization_enabled": (settings or {}).get("folder_organization", (settings or {}).get("organization_enabled", False)),
                       "folder_template": (settings or {}).get("folder_template", "")}
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Перетащите локальные файлы сюда. Все изменения сначала показываются в предпросмотре." if local_files else
                                "Имена выбранных файлов будут использованы при загрузке."))
        preset_line = QHBoxLayout()
        self.preset = QComboBox()
        self.preset.addItem("Свой шаблон", "")
        for name in self._presets.get("presets", {}):
            self.preset.addItem(name, name)
        preset_line.addWidget(QLabel("Пресет"))
        preset_line.addWidget(self.preset, 1)
        save = QPushButton("Сохранить пресет")
        save.clicked.connect(self.save_preset)
        preset_line.addWidget(save)
        delete = QPushButton("Удалить пресет")
        delete.clicked.connect(self.delete_preset)
        preset_line.addWidget(delete)
        layout.addLayout(preset_line)
        form = QFormLayout()
        self.enabled = QCheckBox("Переименовывать файлы")
        form.addRow(self.enabled)
        self.template = QLineEdit()
        form.addRow("Шаблон имени", self.template)
        variables = QLabel("Переменные: {title}, {filename}, {author}, {channel}, {artist}, {album}, "
                           "{track_number:02}, {date}, {source}, {source_id}, {collection}, {number:03}, {ext}")
        variables.setWordWrap(True)
        form.addRow(variables)
        numbering_line = QHBoxLayout()
        self.start = QSpinBox(); self.start.setRange(0, 999999999)
        self.step = QSpinBox(); self.step.setRange(1, 999999999)
        self.padding = QSpinBox(); self.padding.setRange(0, 12)
        for title, widget in (("Начало", self.start), ("Шаг", self.step), ("Разрядность", self.padding)):
            numbering_line.addWidget(QLabel(title)); numbering_line.addWidget(widget)
        form.addRow("Нумерация", numbering_line)
        self.organize = QCheckBox("Распределить по папкам")
        form.addRow(self.organize)
        self.folder_template = QLineEdit()
        self.folder_template.setPlaceholderText("{source}/{collection} или {artist}/{album}")
        form.addRow("Шаблон папки", self.folder_template)
        output_line = QHBoxLayout()
        self.output = QLineEdit(str(output_dir))
        output_line.addWidget(self.output, 1)
        browse = QPushButton("Выбрать…"); browse.clicked.connect(self.choose_output)
        output_line.addWidget(browse)
        form.addRow("Папка вывода", output_line)
        self.collision = QComboBox()
        for title, policy in (("Спросить", "ask"), ("Пропустить", "skip"), ("Перезаписать", "overwrite"), ("Добавить номер", "append_number")):
            self.collision.addItem(title, policy)
        form.addRow("Конфликты имён", self.collision)
        layout.addLayout(form)
        rule_line = QHBoxLayout(); rule_line.addWidget(QLabel("Правила применяются сверху вниз"), 1)
        for title, callback in (("Добавить", self.add_rule), ("Удалить", self.remove_rule),
                                ("↑", lambda: self.move_rule(-1)), ("↓", lambda: self.move_rule(1))):
            btn = QPushButton(title); btn.clicked.connect(callback); rule_line.addWidget(btn)
        layout.addLayout(rule_line)
        self.rules = QTableWidget(0, 3)
        self.rules.setHorizontalHeaderLabels(("Действие", "Найти / символы", "Текст / замена"))
        self.rules.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.rules.setMaximumHeight(135)
        layout.addWidget(self.rules)
        self.preview = QTableView()
        self.preview_model = RenamePreviewModel(self)
        self.preview.setModel(self.preview_model)
        self.preview.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.preview, 1)
        self.status = QLabel(); self.status.setWordWrap(True); layout.addWidget(self.status)
        controls = QHBoxLayout()
        if local_files:
            add = QPushButton("Добавить файлы…"); add.clicked.connect(self.choose_files); controls.addWidget(add)
        controls.addStretch()
        self.cancel_button = QPushButton("Отмена"); self.cancel_button.clicked.connect(self.reject)
        controls.addWidget(self.cancel_button)
        self.apply_button = QPushButton("Переименовать файлы" if local_files else "Применить к загрузке")
        self.apply_button.setObjectName("primary"); self.apply_button.clicked.connect(self.apply_changes)
        controls.addWidget(self.apply_button); layout.addLayout(controls)
        self._timer = QTimer(self); self._timer.setSingleShot(True); self._timer.setInterval(180)
        self._timer.timeout.connect(self.refresh_preview)
        self.set_options(initial)
        for widget in (self.template, self.folder_template, self.output):
            widget.textChanged.connect(self.schedule_preview)
        for widget in (self.start, self.step, self.padding):
            widget.valueChanged.connect(self.schedule_preview)
        for widget in (self.enabled, self.organize):
            widget.toggled.connect(self.schedule_preview)
        self.collision.currentIndexChanged.connect(self.schedule_preview)
        self.rules.itemChanged.connect(self.schedule_preview)
        self.preset.currentIndexChanged.connect(self.load_preset)
        self.schedule_preview()

    @staticmethod
    def _item(item):
        if isinstance(item, (str, Path)):
            return {"path": str(item), "filename": Path(item).name}
        return dict(item)

    def collect_rules(self):
        rows = []
        for index in range(self.rules.rowCount()):
            kind = self.rules.cellWidget(index, 0).currentData()
            first = self.rules.item(index, 1).text() if self.rules.item(index, 1) else ""
            second = self.rules.item(index, 2).text() if self.rules.item(index, 2) else ""
            if kind == "replace":
                rows.append({"type": kind, "from": first, "to": second})
            elif kind == "remove_chars":
                rows.append({"type": kind, "characters": first})
            elif kind == "replace_spaces":
                rows.append({"type": kind, "to": second})
            else:
                rows.append({"type": kind, "value": second or first})
        return rows

    def collect_options(self):
        return {"enabled": self.enabled.isChecked(), "template": self.template.text(),
                "rules": self.collect_rules(), "start": self.start.value(), "step": self.step.value(),
                "padding": self.padding.value(), "organization_enabled": self.organize.isChecked(),
                "folder_template": self.folder_template.text(), "collision_policy": self.collision.currentData(),
                "preset": self.preset.currentData() or ""}

    @property
    def output_dir(self):
        return self.output.text()

    def set_options(self, options):
        self.enabled.setChecked(options.get("enabled", True))
        self.template.setText(options.get("template", "{title}"))
        self.start.setValue(int(options.get("start", 1))); self.step.setValue(int(options.get("step", 1)))
        self.padding.setValue(int(options.get("padding", 0)))
        self.organize.setChecked(options.get("organization_enabled", False))
        self.folder_template.setText(options.get("folder_template", ""))
        index = self.collision.findData(options.get("collision_policy", "ask"))
        self.collision.setCurrentIndex(max(0, index))
        self.rules.setRowCount(0)
        for rule in options.get("rules", []):
            self.add_rule(rule)

    def add_rule(self, rule=None):
        rule = rule if isinstance(rule, dict) else {"type": "replace"}
        index = self.rules.rowCount(); self.rules.insertRow(index)
        action = QComboBox()
        for title, value in (("Заменить", "replace"), ("Добавить в начало", "prefix"), ("Добавить в конец", "suffix"),
                             ("Удалить текст", "remove"), ("Удалить символы", "remove_chars"), ("Заменить пробелы", "replace_spaces")):
            action.addItem(title, value)
        action.setCurrentIndex(max(0, action.findData(rule.get("type", "replace"))))
        self.rules.setCellWidget(index, 0, action)
        self.rules.setItem(index, 1, QTableWidgetItem(str(rule.get("from", rule.get("characters", "")))))
        self.rules.setItem(index, 2, QTableWidgetItem(str(rule.get("to", rule.get("value", "")))))
        action.currentIndexChanged.connect(self.schedule_preview)
        self.schedule_preview()

    def remove_rule(self):
        row = self.rules.currentRow()
        if row >= 0:
            self.rules.removeRow(row); self.schedule_preview()

    def move_rule(self, offset):
        row = self.rules.currentRow(); target = row + offset
        if 0 <= row < self.rules.rowCount() and 0 <= target < self.rules.rowCount():
            rules = self.collect_rules(); rules[row], rules[target] = rules[target], rules[row]
            self.rules.setRowCount(0)
            for rule in rules:
                self.add_rule(rule)
            self.rules.selectRow(target)

    def schedule_preview(self, *args):
        if self.applying:
            return
        self._generation += 1
        self.preview_pending = True
        self.apply_button.setEnabled(False) if hasattr(self, "apply_button") else None
        if hasattr(self, "_timer"):
            self._timer.start()

    def refresh_preview(self):
        if self.applying:
            return
        generation = self._generation
        options, items, output = self.collect_options(), list(self.items), self.output.text()
        self.status.setText("Создаём предпросмотр…")
        self._run(generation, lambda: (options, self.service.preview(items, options, output, self.local_files)), self._preview_done)

    def _run(self, generation, callback, receiver):
        worker = _Worker(generation, callback)
        self._workers[id(worker)] = (worker, receiver)
        worker.signals.completed.connect(self._dispatch)
        self._pool.start(worker)

    @Slot(object, object, object)
    def _dispatch(self, worker_id, result, error):
        entry = self._workers.pop(worker_id, None)
        if entry:
            worker, receiver = entry
            receiver(worker.generation, result, error)

    def _preview_done(self, generation, result, error):
        if generation != self._generation or self.applying:
            return
        self.preview_pending = False
        if error:
            self.plan = []; self.status.setText(error); self.apply_button.setEnabled(False); return
        self.options, self.plan = result
        self.preview_model.set_rows(self.plan)
        conflicts = sum(row["status"] in {"error", "conflict"} for row in self.plan)
        skipped = sum(row["skip"] for row in self.plan)
        self.status.setText(f"Файлов: {len(self.plan)}. Пропусков: {skipped}. Конфликтов: {conflicts}." +
                            (" Выберите другую политику или исправьте шаблон." if conflicts else "") +
                            ("\n" + self.preset_warning if self.preset_warning else ""))
        self.apply_button.setEnabled(bool(self.plan) and not conflicts)
        self.preview_ready.emit(self.plan)

    def apply_changes(self):
        if self.preview_pending or self.applying or not self.plan or any(row["status"] in {"error", "conflict"} for row in self.plan):
            return
        if not self.local_files:
            self.accept(); return
        changed = sum(row["status"] == "ready" for row in self.plan)
        reply = QMessageBox.question(self, "Подтвердите переименование", f"Применить показанные изменения к {changed} файлам?" +
                                     (" Существующие файлы будут перезаписаны." if self.options.get("collision_policy") == "overwrite" else ""),
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.applying = True
        self.apply_button.setEnabled(False); self.cancel_button.setEnabled(False)
        self.status.setText("Переименовываем и распределяем файлы…")
        self._run(self._generation, lambda: self.service.apply(deepcopy(self.plan)), self._apply_done)

    def _apply_done(self, generation, result, error):
        self.applying = False; self.cancel_button.setEnabled(True)
        if error:
            self.status.setText(error); self.schedule_preview(); return
        self.mapping = result; self.applied.emit(result); self.accept()

    def load_preset(self):
        name = self.preset.currentData()
        if name:
            self.set_options(self._presets["presets"][name]); self.schedule_preview()

    def save_preset(self):
        name, accepted = QInputDialog.getText(self, "Сохранить пресет", "Имя пресета:", text=self.preset.currentData() or "")
        if not accepted or not name.strip():
            return
        options = self.collect_options(); name = name.strip()
        updated = deepcopy(self._presets)
        updated["presets"][name] = options
        if self._preset_store.path:
            try:
                self._preset_store.save(updated)
            except (OSError, ValueError, UmdError) as error:
                QMessageBox.warning(self, "Пресет не сохранён", str(error)); return
        self._presets = updated
        index = self.preset.findData(name)
        if index < 0:
            self.preset.addItem(name, name); index = self.preset.count() - 1
        self.preset.setCurrentIndex(index)

    def delete_preset(self):
        name = self.preset.currentData()
        if not name:
            return
        updated = deepcopy(self._presets)
        updated["presets"].pop(name, None)
        if self._preset_store.path:
            try:
                self._preset_store.save(updated)
            except (OSError, ValueError, UmdError) as error:
                QMessageBox.warning(self, "Пресет не удалён", str(error)); return
        self._presets = updated
        self.preset.removeItem(self.preset.currentIndex()); self.schedule_preview()

    def choose_output(self):
        selected = QFileDialog.getExistingDirectory(self, "Папка вывода", self.output.text())
        if selected:
            self.output.setText(selected)

    def choose_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Файлы для переименования")
        self.add_files(files)

    def add_files(self, paths):
        known = {str(Path(item.get("path", "")).resolve()).casefold() for item in self.items if item.get("path")}
        for path in paths:
            file = Path(path)
            key = str(file.resolve()).casefold()
            if file.is_file() and key not in known:
                self.items.append({"path": str(file.resolve()), "filename": file.name}); known.add(key)
        self.schedule_preview()

    def dragEnterEvent(self, event):
        if self.local_files and event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        if self.local_files:
            self.add_files([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])
            event.acceptProposedAction()

    def reject(self):
        if not self.applying:
            self._generation += 1; self._timer.stop(); super().reject()

    def closeEvent(self, event):
        if self.applying:
            event.ignore()
        else:
            self._generation += 1; self._timer.stop(); super().closeEvent(event)
