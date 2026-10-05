"""Verify TLS provenance independently of the build host's CPython distribution."""

import pytest

from scripts.build_windows import verify_ssl_runtime


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
