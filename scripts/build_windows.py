"""Build, smoke-test and archive the Windows x64 desktop distribution."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def run(*command, **kwargs):
    subprocess.run(command, cwd=ROOT, check=True, **kwargs)


def diagnostic(executable, package, arguments, report):
    report.unlink(missing_ok=True)
    subprocess.run([str(executable), *arguments, "--diagnostics-output", str(report)],
                   cwd=package, check=True, timeout=120)
    if not report.is_file():
        raise RuntimeError("Windowless executable did not write its diagnostic result")
    result = json.loads(report.read_text(encoding="utf-8"))
    if result.get("ok") is not True or result.get("exit_code") != 0:
        raise RuntimeError(f"Packaged executable diagnostic failed: {result}")
    return result


def pyinstaller_spec():
    """Two launchers share one dependency bundle; the normal launcher has no console."""
    build_dir = ROOT / "build"
    build_dir.mkdir(exist_ok=True)
    spec = build_dir / "UMD-release.spec"
    spec.write_text(
        "from PyInstaller.utils.hooks import collect_all\n"
        "from pathlib import Path\n"
        "datas, binaries, hiddenimports = collect_all('playwright')\n"
        f"a = Analysis([{str(ROOT / 'main.py')!r}], pathex=[{str(ROOT)!r}], "
        "binaries=binaries, datas=datas, hiddenimports=hiddenimports, "
        "hookspath=[], runtime_hooks=[], excludes=[])\n"
        "# Windows 10/11 supplies these OS libraries; ambient PATH copies may have incompatible exports.\n"
        "def system_library(name):\n"
        "    name = Path(name).name.lower()\n"
        "    return (name.startswith(('api-ms-win-', 'ext-ms-win-', 'icudt')) "
        "or name in {'ucrtbase.dll', 'icu.dll', 'icuuc.dll', 'icuin.dll'})\n"
        "a.binaries = [entry for entry in a.binaries if not system_library(entry[0])]\n"
        "pyz = PYZ(a.pure)\n"
        "gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name='UMD', "
        "debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False)\n"
        "cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name='UMD-console', "
        "debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=True)\n"
        "collect = COLLECT(gui, cli, a.binaries, a.datas, strip=False, upx=False, name='UMD')\n",
        encoding="utf-8")
    return spec


def isolate_build_path():
    """Resolve only this interpreter's libraries, bundled tools and Windows system DLLs."""
    git_executable = shutil.which("git")
    if not git_executable:
        raise RuntimeError("Git is required for release provenance")
    windows = Path(os.environ["SystemRoot"])
    directories = [Path(sys.executable).parent, Path(sys.base_prefix),
                   Path(sys.base_prefix) / "DLLs", Path(sys.prefix) / "Scripts",
                   windows / "System32", windows, Path(git_executable).parent, ROOT / "tools"]
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys(str(path) for path in directories))


def verify_ssl_runtime(package, interpreter_root=None):
    """Check collected TLS DLLs against this interpreter, allowing CPython naming variants."""
    interpreter = Path(interpreter_root or sys.base_prefix)
    internal = Path(package) / "_internal"
    for family in ("libssl-3", "libcrypto-3"):
        sources = {}
        for directory in (interpreter / "DLLs", interpreter):
            for source in directory.glob(f"{family}*.dll"):
                if not source.is_file():
                    continue
                name = source.name.lower()
                digest = hashlib.sha256(source.read_bytes()).digest()
                if name in sources and sources[name] != digest:
                    raise RuntimeError(f"Ambiguous interpreter OpenSSL dependency: {source.name}")
                sources[name] = digest
        bundled = [path for path in internal.glob(f"{family}*.dll") if path.is_file()]
        if not sources or not bundled:
            raise RuntimeError(f"Missing interpreter OpenSSL dependency: {family}*.dll")
        for library in bundled:
            expected = sources.get(library.name.lower())
            if expected is None or hashlib.sha256(library.read_bytes()).digest() != expected:
                raise RuntimeError(
                    f"Incompatible build-host TLS dependency entered the package: {library.name}")


def build(version, tag):
    if platform.system() != "Windows" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("Windows x64 Python is required to build this release")
    if not re.fullmatch(r"\d+\.\d+\.\d+(b[1-9][0-9]*)?", version):
        raise ValueError("Invalid build version")
    expected_tag = "v" + re.sub(r"b([0-9]+)$", r"-beta.\1", version)
    if tag != expected_tag:
        raise ValueError("Tag does not match the package version")
    isolate_build_path()
    os.environ["SETUPTOOLS_SCM_PRETEND_VERSION"] = version
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
    # Regenerate the version module used by the frozen executable.
    run(sys.executable, "-m", "pip", "install", "--no-deps", "--no-build-isolation", "--editable", ".")
    build_directory = ROOT / "build"
    build_directory.mkdir(exist_ok=True)
    # Isolate each build's tests from other runs and restricted shared Windows temp folders.
    with tempfile.TemporaryDirectory(prefix="pytest-release-", dir=build_directory) as tests_directory:
        if not Path(tests_directory).resolve().is_relative_to(build_directory.resolve()):
            raise RuntimeError("Test temporary directory escaped the build workspace")
        run(sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider",
            "--basetemp", tests_directory)
    run(sys.executable, "-m", "unittest", "discover", "-s", ".github/tests", "-q")
    for tool in ("yt-dlp.exe", "deno.exe", "ffmpeg.exe", "ffprobe.exe"):
        if not (ROOT / "tools" / tool).is_file():
            raise RuntimeError("Run python scripts/provision_tools.py before building")
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build" / "pyinstaller"),
        str(pyinstaller_spec()))
    package = ROOT / "dist" / "UMD"
    # Qt's TLS backend and Python must load the same compatible OpenSSL build.
    verify_ssl_runtime(package)
    shutil.copytree(ROOT / "tools", package / "tools", dirs_exist_ok=True)
    # The build runner can use a different Python/OpenSSL patch version from provisioning.
    from provision_tools import runtime_notices
    runtime_versions = runtime_notices(package / "tools" / "licenses")
    shutil.copy2(ROOT / "README.md", package / "README.md")
    shutil.copy2(ROOT / "docs" / "THIRD_PARTY.md", package / "THIRD_PARTY.md")
    shutil.copy2(ROOT / "release-notes" / (version.split("b")[0] + ".md"), package / "RELEASE_NOTES.md")
    (package / "START_HERE.txt").write_text(
        "UMD — Universal Media Downloader\n\n"
        "Windows 10/11 x64. Распакуйте ВЕСЬ архив, затем запустите UMD.exe.\n"
        "UMD.exe открывает графический интерфейс: вставьте URL, Analyze, выберите параметры и Download.\n"
        "Python, Qt, yt-dlp, Deno, FFmpeg/FFprobe и Chromium входят в сборку.\n"
        "Настройки/прогресс: %LOCALAPPDATA%\\UMD. Экспорт: подкаталог output.\n"
        "Очередь и история сохраняются между запусками.\n"
        "UMD-console.exe — отдельная консоль для диагностики и автоматизации.\n"
        "Для проверки: UMD-console.exe --check-environment.\n"
        "Подробности и ограничения: README.md.\n", encoding="utf-8")
    executable = package / "UMD.exe"
    console = package / "UMD-console.exe"
    actual = subprocess.check_output([str(console), "--version"], cwd=package, text=True, timeout=120).strip()
    if actual != version:
        raise RuntimeError(f"Executable version {actual!r} differs from requested {version!r}")
    for tool in ("ffmpeg.exe", "ffprobe.exe"):
        result = subprocess.check_output([str(package / "tools" / tool), "-version"],
                                         cwd=package, text=True, timeout=30)
        if not result.startswith(tool.removesuffix(".exe") + " version "):
            raise RuntimeError(f"Invalid bundled media tool: {tool}")
    verification = ROOT / "build" / "verification"
    verification.mkdir(exist_ok=True)
    version_result = diagnostic(executable, package, ["--version"], verification / "version.json")
    if version_result["stdout"] != version:
        raise RuntimeError("Windowless executable returned a different version")
    diagnostic(executable, package, ["--self-test"], verification / "self-test.json")
    with tempfile.TemporaryDirectory(prefix="umd-build-smoke-") as directory:
        diagnostic(executable, package, ["--data-dir", directory, "--check-environment"],
                   verification / "environment.json")
        gui_report = verification / "gui-smoke.json"
        screenshot = verification / "gui-smoke.png"
        gui_report.unlink(missing_ok=True)
        screenshot.unlink(missing_ok=True)
        gui_environment = dict(os.environ)
        gui_environment.pop("QT_QPA_PLATFORM", None)  # Verify the real Windows platform plugin.
        subprocess.run([str(executable), "--data-dir", directory, "--gui-smoke",
                        "--diagnostics-output", str(gui_report), "--screenshot", str(screenshot)],
                       cwd=package, check=True, timeout=120, env=gui_environment)
        result = json.loads(gui_report.read_text(encoding="utf-8"))
        if (result.get("ok") is not True or result.get("window_visible") is not True
                or result.get("version") != version or result.get("queue_initialized") is not True
                or len(result.get("tabs", [])) < 4 or not screenshot.is_file()
                or screenshot.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("Packaged GUI failed visible-window/event-loop/queue smoke verification")
    output = ROOT / "release-dist"
    # All cleanup is constrained to this explicit build output, never user data.
    output.mkdir(exist_ok=True)
    for path in output.iterdir():
        if path.is_file():
            path.unlink()
        else:
            raise RuntimeError("Unexpected directory in release-dist; cleanup manually")
    filename = f"UMD-{tag}-windows-x64.zip"
    archive_path = output / filename
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                archive.write(path, "UMD/" + path.relative_to(package).as_posix())
    with archive_path.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    manifest = {"version": version, "tag": tag, "platform": "windows-x64", "commit": commit,
                "executable": "UMD/UMD.exe", "sha256": digest,
                "console_executable": "UMD/UMD-console.exe", "ui": "qt-widgets",
                "runtime_versions": runtime_versions,
                "smoke_tests": ["--version", "--self-test", "--check-environment", "--gui-smoke"],
                "checks": {"version": True, "self_test": True, "environment": True, "unit_tests": True,
                           "gui": True, "media_tools": True, "ssl_runtime": True}}
    (output / "build-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (output / "SHA256SUMS.txt").write_text(f"{digest}  {filename}\n", encoding="utf-8")
    print(f"Built and verified {archive_path} ({archive_path.stat().st_size / 1024**2:.1f} MiB)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    build(args.version, args.tag)
