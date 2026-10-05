"""Windowed application launcher and packaged GUI smoke verification."""
import argparse
import json
import logging
from pathlib import Path
import sys

from PySide6.QtCore import QTimer
from PySide6.QtNetwork import QSslSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from app import __version__
from app.core.config import SettingsStore
from app.ui.cli import setup_logging


def configure_tls_backend():
    """Windows certificate validation and HTTPS use the native Schannel backend."""
    if sys.platform == "win32" and not QSslSocket.setActiveBackend("schannel"):
        raise RuntimeError("Windows Schannel TLS backend is unavailable")
    if not QSslSocket.supportsSsl():
        raise RuntimeError("Qt HTTPS support is unavailable")
    return {"tls_backend": QSslSocket.activeBackend(), "tls_supported": True}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="UMD")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--gui-smoke", nargs="?", const="")
    parser.add_argument("--diagnostics-output", type=Path)
    parser.add_argument("--screenshot", "--gui-screenshot", type=Path)
    args = parser.parse_args(argv)
    application = QApplication.instance() or QApplication(["UMD"])
    application.setApplicationName("UMD")
    application.setOrganizationName("UMD")
    application.setStyle("Fusion")
    report_path = Path(args.gui_smoke) if args.gui_smoke else args.diagnostics_output
    try:
        tls = configure_tls_backend()
        store = SettingsStore(args.data_dir.resolve() if args.data_dir else None)
        setup_logging(store.data_dir, store.load())
        from app.ui.window import MainWindow
        window = MainWindow(store)
        window.show()
    except Exception as error:
        logging.exception("Application startup failed")
        if args.gui_smoke is not None and report_path:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps({"ok": False, "error": str(error)}), encoding="utf-8")
        else:
            QMessageBox.critical(None, "UMD — ошибка запуска", str(error) + "\nПроверьте настройки и файлы сборки.")
        return 1
    if args.gui_smoke is not None:
        result = {"ok": False}
        def verify():
            try:
                application.processEvents()
                if args.screenshot:
                    args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                    if not window.grab().save(str(args.screenshot), "PNG"):
                        raise RuntimeError("Не удалось сохранить снимок окна")
                result.update(ok=window.isVisible() and window.pages.count() == 5,
                              window_visible=window.isVisible(), version=__version__,
                              tabs=window.page_names, queue_initialized=window.queue is not None, **tls)
            except Exception as error:
                result.update(ok=False, error=str(error))
            finally:
                if report_path:
                    report_path.parent.mkdir(parents=True, exist_ok=True)
                    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                window.close()
                application.quit()
        QTimer.singleShot(1000, verify)
        application.exec()
        return 0 if result["ok"] else 1
    return application.exec()
