"""Qt must fail clearly without HTTPS and use Windows certificate validation."""
import sys

import pytest
from PySide6.QtWidgets import QApplication

from app.ui import gui


def test_actual_windows_qt_supports_https_with_schannel():
    if sys.platform != "win32":
        pytest.skip("Windows Schannel backend")
    application = QApplication.instance() or QApplication(["UMD-TLS-tests"])
    assert gui.configure_tls_backend() == {"tls_backend": "schannel", "tls_supported": True}
    assert application is not None


def test_unavailable_native_backend_fails_startup(monkeypatch):
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(gui.QSslSocket, "setActiveBackend", lambda backend: False)
    with pytest.raises(RuntimeError, match="Schannel TLS backend is unavailable"):
        gui.configure_tls_backend()


def test_missing_https_support_fails_startup(monkeypatch):
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(gui.QSslSocket, "setActiveBackend", lambda backend: True)
    monkeypatch.setattr(gui.QSslSocket, "supportsSsl", lambda: False)
    with pytest.raises(RuntimeError, match="HTTPS support is unavailable"):
        gui.configure_tls_backend()
