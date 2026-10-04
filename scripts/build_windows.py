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


def build(version, tag):
    if platform.system() != "Windows" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("Windows x64 Python is required to build this release")
    if not re.fullmatch(r"\d+\.\d+\.\d+(b[1-9][0-9]*)?", version):
        raise ValueError("Invalid build version")
    expected_tag = "v" + re.sub(r"b([0-9]+)$", r"-beta.\1", version)
    if tag != expected_tag:
        raise ValueError("Tag does not match the package version")
    os.environ["SETUPTOOLS_SCM_PRETEND_VERSION"] = version
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
    # Regenerate the version module used by the frozen executable.
    run(sys.executable, "-m", "pip", "install", "--no-deps", "--no-build-isolation", "--editable", ".")
    run(sys.executable, "-m", "pytest", "tests", "-q")
    run(sys.executable, "-m", "unittest", "discover", "-s", ".github/tests", "-q")
    for tool in ("yt-dlp.exe", "deno.exe"):
        if not (ROOT / "tools" / tool).is_file():
            raise RuntimeError("Run python scripts/provision_tools.py before building")
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--console",
        "--name", "UMD", "--collect-all", "playwright", "--paths", str(ROOT), "main.py")
    package = ROOT / "dist" / "UMD"
    shutil.copytree(ROOT / "tools", package / "tools", dirs_exist_ok=True)
    shutil.copy2(ROOT / "README.md", package / "README.md")
    shutil.copy2(ROOT / "docs" / "THIRD_PARTY.md", package / "THIRD_PARTY.md")
    shutil.copy2(ROOT / "release-notes" / (version.split("b")[0] + ".md"), package / "RELEASE_NOTES.md")
    (package / "START_HERE.txt").write_text(
        "UMD — Universal Media Downloader\n\n"
        "Windows 10/11 x64. Распакуйте ВЕСЬ архив, затем запустите UMD.exe.\n"
        "Первая бета: консольное меню, сбор и экспорт метаданных YouTube.\n"
        "Python, yt-dlp, Deno и Chromium входят в сборку.\n"
        "Настройки/прогресс: %LOCALAPPDATA%\\UMD. Экспорт: подкаталог output.\n"
        "Закрытие окна не удаляет прогресс: выберите Продолжить предыдущую.\n"
        "Для проверки: UMD.exe --check-environment.\n"
        "Подробности и ограничения: README.md.\n", encoding="utf-8")
    executable = package / "UMD.exe"
    actual = subprocess.check_output([str(executable), "--version"], cwd=package, text=True).strip()
    if actual != version:
        raise RuntimeError(f"Executable version {actual!r} differs from requested {version!r}")
    run(str(executable), "--self-test")
    with tempfile.TemporaryDirectory(prefix="umd-build-smoke-") as directory:
        run(str(executable), "--data-dir", directory, "--check-environment")
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
                "smoke_tests": ["--version", "--self-test", "--check-environment"],
                "checks": {"version": True, "self_test": True, "environment": True, "unit_tests": True}}
    (output / "build-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (output / "SHA256SUMS.txt").write_text(f"{digest}  {filename}\n", encoding="utf-8")
    print(f"Built and verified {archive_path} ({archive_path.stat().st_size / 1024**2:.1f} MiB)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    build(args.version, args.tag)
