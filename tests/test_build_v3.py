import hashlib
import json
import runpy
import sys
from types import ModuleType, SimpleNamespace
import zipfile

import pytest

from scripts import build_windows as builder
from scripts import provision_tools as provision


def test_generated_spec_collects_rename_presets_at_runtime_module_path(monkeypatch, tmp_path):
    # Exercise our generated spec without requiring the optional build runtime
    # or collecting the developer machine's real browser installation.
    installer = ModuleType("PyInstaller")
    installer.__path__ = []
    utilities = ModuleType("PyInstaller.utils")
    utilities.__path__ = []
    hooks = ModuleType("PyInstaller.utils.hooks")
    browser_data = [("browser-fixture", "playwright")]
    hooks.collect_all = lambda package: (list(browser_data), [], [package])
    installer.utils = utilities
    utilities.hooks = hooks
    for module in (installer, utilities, hooks):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    root = tmp_path / "source"
    root.mkdir()
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setitem(sys.modules, "build_windows", builder)
    captures = []
    def analysis(*args, **kwargs):
        captures.extend(kwargs["datas"])
        return SimpleNamespace(binaries=[], pure=[], scripts=[], datas=kwargs["datas"])
    spec = builder.pyinstaller_spec()
    runpy.run_path(str(spec), init_globals={"Analysis": analysis, "PYZ": lambda _: None,
                                          "EXE": lambda *args, **kwargs: None,
                                          "COLLECT": lambda *args, **kwargs: None})
    assert (str(root / "app/rename/default_presets.json"), "app/rename") in captures
    assert browser_data[0] in captures


@pytest.fixture
def package(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "provision_tools", provision)
    payload = b"verified gallery tool fixture"
    monkeypatch.setattr(provision, "GALLERY_DL_SHA256", hashlib.sha256(payload).hexdigest())
    tool = tmp_path / "tools/gallery-dl.exe"
    tool.parent.mkdir()
    tool.write_bytes(payload)
    presets = tmp_path / "_internal/app/rename/default_presets.json"
    presets.parent.mkdir(parents=True)
    presets.write_text(json.dumps({"version": 1, "presets": {"Photo": {"template": "{title}"}}}), encoding="utf-8")
    licenses = tmp_path / "tools/licenses"
    licenses.mkdir()
    for name in ("gallery-dl-LICENSE.txt", "gallery-dl-SOURCE.txt", "python-LICENSE.txt", "openssl-LICENSE.txt"):
        (licenses / name).write_text("Fixture dependency attribution", encoding="utf-8")
    monkeypatch.setattr(builder.subprocess, "check_output", lambda *args, **kwargs: provision.GALLERY_DL_VERSION + "\n")
    return tmp_path


def test_frozen_gallery_package_verifies_tool_version_checksum_presets_and_notices(package):
    assert builder.verify_gallery_package(package) == provision.GALLERY_DL_VERSION


@pytest.mark.parametrize("missing", ["tools/gallery-dl.exe", "_internal/app/rename/default_presets.json",
                                     "tools/licenses/gallery-dl-LICENSE.txt", "tools/licenses/gallery-dl-SOURCE.txt"])
def test_missing_tool_preset_or_license_refuses_packaging(package, missing):
    (package / missing).unlink()
    with pytest.raises(RuntimeError, match="missing"):
        builder.verify_gallery_package(package)


def test_corrupted_binary_is_rejected_even_if_reported_version_is_correct(package):
    (package / "tools/gallery-dl.exe").write_bytes(b"corrupted binary")
    with pytest.raises(RuntimeError, match="checksum"):
        builder.verify_gallery_package(package)


def test_unexpected_gallery_version_is_rejected(package, monkeypatch):
    monkeypatch.setattr(builder.subprocess, "check_output", lambda *args, **kwargs: "different version")
    with pytest.raises(RuntimeError, match="version differs"):
        builder.verify_gallery_package(package)


def test_archive_must_contain_nonempty_gallery_resources_and_license(package, tmp_path):
    archive = tmp_path / "portable.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for path in package.rglob("*"):
            if path.is_file():
                output.write(path, "UMD/" + path.relative_to(package).as_posix())
    builder.verify_gallery_zip(archive)
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("UMD/tools/gallery-dl.exe", b"exe fixture")
    with pytest.raises(RuntimeError, match="Portable ZIP"):
        builder.verify_gallery_zip(archive)


@pytest.mark.parametrize("tool_ok", [True, False])
def test_environment_gallery_check_uses_no_auth_arguments_and_redacts_failure(monkeypatch, tmp_path, capsys, tool_ok):
    from app.core.config import Settings
    from app.ui import diagnostics
    from app.downloader import ffmpeg
    from app.localization import youtube
    from app.sources import gallery_dl_source
    monkeypatch.setattr(diagnostics, "YtDlp", lambda settings: SimpleNamespace(check=lambda: "fixture", runtime_version=lambda: "fixture"))
    monkeypatch.setattr(ffmpeg, "FFmpegProcessor", lambda settings: SimpleNamespace(check=lambda: "fixture"))
    monkeypatch.setattr(youtube, "check_browser", lambda settings: {"ok": True, "message": "fixture"})
    monkeypatch.setattr(gallery_dl_source, "GalleryDlSource", lambda settings: SimpleNamespace(executable="gallery-dl.exe"))
    commands = []
    def version(command):
        commands.append(command)
        return (0, provision.GALLERY_DL_VERSION) if tool_ok else (1, "session-secret-should-not-print")
    monkeypatch.setattr(ffmpeg, "run_process", version)
    settings = Settings(cookie_file=str(tmp_path / "private-cookies.txt"), auth_mode="cookies")
    assert diagnostics.environment_check(settings, tmp_path) is tool_ok
    assert commands == [["gallery-dl.exe", "--version"]]
    output = capsys.readouterr().out
    assert "gallery-dl" in output
    assert "private-cookies" not in output and "session-secret" not in output
