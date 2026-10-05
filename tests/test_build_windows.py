"""Verify TLS provenance independently of the build host's CPython distribution."""

import pytest

from scripts.build_windows import filter_windows_binaries, isolate_build_path, verify_ssl_runtime


def runtime(tmp_path, suffix="", location="DLLs"):
    interpreter = tmp_path / "python"
    source = interpreter / location
    source.mkdir(parents=True)
    package = tmp_path / "package"
    internal = package / "_internal"
    internal.mkdir(parents=True)
    for family in ("libssl-3", "libcrypto-3"):
        name = f"{family}{suffix}.dll"
        payload = ("Interpreter DLL " + name).encode()
        (source / name).write_bytes(payload)
        (internal / name).write_bytes(payload)
    return interpreter, package, source, internal


@pytest.mark.parametrize("suffix", ["", "-x64"])
@pytest.mark.parametrize("location", ["DLLs", ""])
def test_accepts_actual_cpython_dll_names_and_locations(tmp_path, suffix, location):
    interpreter, package, _, _ = runtime(tmp_path, suffix, location)
    verify_ssl_runtime(package, interpreter)


@pytest.mark.parametrize("family", ["libssl-3", "libcrypto-3"])
def test_rejects_build_host_library_with_same_name(tmp_path, family):
    interpreter, package, _, internal = runtime(tmp_path)
    (internal / f"{family}.dll").write_bytes(b"Incompatible ambient DLL")
    with pytest.raises(RuntimeError, match="Incompatible build-host TLS"):
        verify_ssl_runtime(package, interpreter)


@pytest.mark.parametrize("missing_from", ["source", "package"])
@pytest.mark.parametrize("family", ["libssl-3", "libcrypto-3"])
def test_rejects_missing_runtime_library(tmp_path, missing_from, family):
    interpreter, package, source, internal = runtime(tmp_path)
    directory = source if missing_from == "source" else internal
    (directory / f"{family}.dll").unlink()
    with pytest.raises(RuntimeError, match="Missing interpreter OpenSSL"):
        verify_ssl_runtime(package, interpreter)


def test_rejects_extra_host_library_with_another_name(tmp_path):
    interpreter, package, _, internal = runtime(tmp_path)
    (internal / "libssl-3-x64.dll").write_bytes(b"Ambient TLS build")
    with pytest.raises(RuntimeError, match="Incompatible build-host TLS"):
        verify_ssl_runtime(package, interpreter)


def test_rejects_conflicting_interpreter_copies(tmp_path):
    interpreter, package, _, _ = runtime(tmp_path)
    (interpreter / "libssl-3.dll").write_bytes(b"Different runtime build")
    with pytest.raises(RuntimeError, match="Ambiguous interpreter OpenSSL"):
        verify_ssl_runtime(package, interpreter)


@pytest.mark.parametrize("suffix", ["", "-x64"])
def test_collects_python_tls_and_native_qt_backend_without_git_openssl(tmp_path, suffix):
    interpreter, package, source, _ = runtime(tmp_path, suffix)
    ambient = tmp_path / "Git" / "mingw64" / "bin"
    ambient.mkdir(parents=True)
    entries = [(f"libssl-3{suffix}.dll", str(source / f"libssl-3{suffix}.dll"), "BINARY"),
               (f"libcrypto-3{suffix}.dll", str(source / f"libcrypto-3{suffix}.dll"), "BINARY"),
               ("libssl-3-x64.dll", str(ambient / "libssl-3-x64.dll"), "BINARY"),
               ("libcrypto-3-x64.dll", str(ambient / "libcrypto-3-x64.dll"), "BINARY"),
               ("PySide6/plugins/tls/qopensslbackend.dll", "qt/qopensslbackend.dll", "BINARY"),
               ("PySide6/plugins/tls/qschannelbackend.dll", "qt/qschannelbackend.dll", "BINARY")]
    assert filter_windows_binaries(entries, interpreter) == [entries[0], entries[1], entries[-1]]
    verify_ssl_runtime(package, interpreter)


def test_git_is_resolved_without_exposing_its_tls_libraries_to_collection(monkeypatch, tmp_path):
    from scripts import build_windows
    git = str(tmp_path / "Git" / "bin" / "git.exe")
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    monkeypatch.setenv("PATH", str(tmp_path / "Git" / "bin"))
    monkeypatch.setattr(build_windows.shutil, "which", lambda name: git)
    assert isolate_build_path() == git
    assert str(tmp_path / "Git") not in build_windows.os.environ["PATH"]
