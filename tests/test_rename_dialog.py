"""Qt preview and local application use the real engine on worker threads."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import threading
import time

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from app.ui.rename_dialog import RenameDialog


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(["UMD-rename-tests"])


def wait(app, predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("Rename worker did not finish")


def test_dialog_pre_download_async_template_preview(qt_app, tmp_path):
    dialog = RenameDialog([{"title": "Фото", "ext": "jpg"}, {"title": "Токио", "ext": "jpg"}], tmp_path)
    main_thread = threading.get_ident()
    threads = []
    original = dialog.service.preview
    def preview(*args):
        threads.append(threading.get_ident())
        return original(*args)
    dialog.service.preview = preview
    dialog.show()
    dialog.template.setText("Отпуск - {number:03}")
    dialog.start.setValue(4)
    dialog.step.setValue(2)
    wait(qt_app, lambda: not dialog.preview_pending and bool(dialog.plan))
    assert [row["filename"] for row in dialog.plan] == ["Отпуск - 004.jpg", "Отпуск - 006.jpg"]
    assert threads and all(worker != main_thread for worker in threads)
    assert dialog.preview_model.rowCount() == 2
    dialog.apply_button.click()
    assert dialog.result() == RenameDialog.DialogCode.Accepted
    assert dialog.options["template"] == "Отпуск - {number:03}"
    assert not list(tmp_path.iterdir())
    dialog.close()


def test_dialog_conflicts_block_apply_until_policy_selected(qt_app, tmp_path):
    items = [{"title": "Same", "ext": "jpg"}, {"title": "Same", "ext": "jpg"}]
    dialog = RenameDialog(items, tmp_path)
    dialog.show()
    wait(qt_app, lambda: not dialog.preview_pending and bool(dialog.plan))
    assert not dialog.apply_button.isEnabled()
    assert "Конфликтов: 1" in dialog.status.text()
    dialog.collision.setCurrentIndex(dialog.collision.findData("append_number"))
    wait(qt_app, lambda: not dialog.preview_pending and dialog.apply_button.isEnabled())
    assert dialog.plan[1]["filename"] == "Same (1).jpg"
    dialog.reject()


def test_dialog_post_download_requires_confirmation_then_renames(qt_app, tmp_path, monkeypatch):
    file = tmp_path / "original.jpg"; file.write_bytes(b"photo")
    dialog = RenameDialog([file], tmp_path, local_files=True)
    dialog.show(); dialog.template.setText("Снимок - {number:03}")
    wait(qt_app, lambda: not dialog.preview_pending and bool(dialog.plan))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    dialog.apply_button.click()
    assert file.exists()
    assert not (tmp_path / "Снимок - 001.jpg").exists()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    dialog.apply_button.click()
    wait(qt_app, lambda: dialog.result() == RenameDialog.DialogCode.Accepted)
    target = tmp_path / "Снимок - 001.jpg"
    assert target.read_bytes() == b"photo"
    assert dialog.mapping == {str(file): str(target)}
    assert not file.exists()
    dialog.close()


def test_local_dialog_add_files_deduplicates_without_metadata(qt_app, tmp_path):
    file = tmp_path / "new.png"; file.write_bytes(b"png")
    dialog = RenameDialog([], tmp_path, local_files=True)
    dialog.add_files([file, file])
    wait(qt_app, lambda: not dialog.preview_pending and bool(dialog.plan))
    assert len(dialog.items) == 1
    assert dialog.plan[0]["filename"] == "new.png"
    assert dialog.plan[0]["status"] == "unchanged"
    dialog.close()


def test_dialog_rule_editor_and_saved_preset(qt_app, tmp_path):
    dialog = RenameDialog([{"filename": "IMG_name.jpg"}], tmp_path, presets=tmp_path / "presets.json")
    dialog.template.setText("{filename}")
    dialog.add_rule({"type": "replace", "from": "IMG_", "to": ""})
    dialog.add_rule({"type": "prefix", "value": "Trip "})
    wait(qt_app, lambda: not dialog.preview_pending and bool(dialog.plan))
    assert dialog.plan[0]["filename"] == "Trip name.jpg"
    dialog._preset_store.upsert("Trip", dialog.collect_options())
    assert dialog._preset_store.get("Trip")["rules"][0]["from"] == "IMG_"
    dialog.close()


def test_stale_async_preview_cannot_enable_apply(qt_app, tmp_path):
    dialog = RenameDialog([{"title": "image", "ext": "jpg"}], tmp_path)
    gate = threading.Event()
    original = dialog.service.preview
    def slow(items, options, *args):
        if options["template"] == "old":
            gate.wait(3)
        return original(items, options, *args)
    dialog.service.preview = slow
    dialog.template.setText("old"); dialog._timer.stop(); dialog.refresh_preview()
    dialog.template.setText("new"); dialog._timer.stop(); dialog.refresh_preview()
    wait(qt_app, lambda: bool(dialog.plan) and dialog.plan[0]["filename"] == "new.jpg")
    gate.set()
    wait(qt_app, lambda: not dialog._workers)
    assert dialog.plan[0]["filename"] == "new.jpg"
    dialog.close()
