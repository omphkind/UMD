"""Fetch pinned, checksum-verified upstream tools into the project tools folder."""

import hashlib
import json
from pathlib import Path
import urllib.request
import zipfile
import io
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
YT_DLP_VERSION = "2026.08.19"
DENO_VERSION = "v2.9.7"
DENO_SHA256 = "a0c3101b4158d1dfb7d6a78a7bf0f3de80c96bb423c152beec8beb22786f2238"
FFMPEG_VERSION = "9.0.2"
# Release essentials ZIP from the Windows distributor linked by ffmpeg.org.
FFMPEG_SHA256 = "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"
FFMPEG_URL = f"https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-{FFMPEG_VERSION}-essentials_build.zip"
GALLERY_DL_RELEASE = "2026.10.05"
GALLERY_DL_VERSION = "1.33.0-dev:2026.10.05"
GALLERY_DL_SHA256 = "a581635fc172f62e099e27ef742de985f363ebb06ea6fdaadd1db2594dd93e05"
GALLERY_DL_URL = f"https://github.com/gdl-org/builds/releases/download/{GALLERY_DL_RELEASE}/gallery-dl_windows.exe"
GALLERY_DL_SOURCE = "https://codeberg.org/mikf/gallery-dl/commit/b11951527bdb6f84e844075dca8332d920376236"
GALLERY_DL_LICENSE_URL = "https://raw.githubusercontent.com/mikf/gallery-dl/v1.32.15/LICENSE"


def download(url):
    request = urllib.request.Request(url, headers={"User-Agent": "UMD-build/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def verify(payload, expected):
    if hashlib.sha256(payload).hexdigest() != expected.lower():
        raise RuntimeError("Official tool checksum verification failed")


def cached_tool(target, url, checksum):
    """Reuse verified files only; a failed download must never replace the previous copy."""
    target = Path(target)
    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == checksum.lower():
        return target
    payload = download(url)
    verify(payload, checksum)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".download")
    try:
        temporary.write_bytes(payload)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def provision_gallery(tools=None):
    tools = Path(tools or TOOLS)
    cached_tool(tools / "gallery-dl.exe", GALLERY_DL_URL, GALLERY_DL_SHA256)
    licenses = tools / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    license_text = download(GALLERY_DL_LICENSE_URL)
    if b"GNU GENERAL PUBLIC LICENSE" not in license_text or b"Version 2" not in license_text:
        raise RuntimeError("Unexpected upstream gallery-dl license text")
    (licenses / "gallery-dl-LICENSE.txt").write_bytes(license_text)
    dependencies = "https://codeberg.org/mikf/gallery-dl/src/commit/b11951527bdb6f84e844075dca8332d920376236/pyproject.toml"
    (licenses / "gallery-dl-SOURCE.txt").write_text(
        f"gallery-dl {GALLERY_DL_VERSION} — Mike Fährmann and contributors.\n"
        "Licensed under GNU GPL version 2; see gallery-dl-LICENSE.txt.\n"
        f"Official standalone Windows build: {GALLERY_DL_URL}\nSHA256: {GALLERY_DL_SHA256}\n"
        f"Exact source: {GALLERY_DL_SOURCE}\n"
        f"Build recipe and standalone dependency references: https://github.com/gdl-org/builds/tree/master\n"
        f"Pinned release/build dependency references: https://github.com/gdl-org/builds/releases/tag/{GALLERY_DL_RELEASE}\n"
        f"Project dependencies: {dependencies}\n"
        f"GPL license text mirror: {GALLERY_DL_LICENSE_URL}\n",
        encoding="utf-8")
    return {"version": GALLERY_DL_VERSION, "release": GALLERY_DL_RELEASE, "sha256": GALLERY_DL_SHA256,
            "binary_source": GALLERY_DL_URL, "source": GALLERY_DL_SOURCE,
            "license_source": GALLERY_DL_LICENSE_URL, "dependencies_source": dependencies}


def runtime_notices(destination):
    """Preserve license texts for the Python/OpenSSL runtime actually being packaged."""
    import ssl

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("Python runtime LICENSE.txt is missing")
    shutil.copy2(python_license, destination / "python-LICENSE.txt")
    match = re.match(r"OpenSSL (3\.[0-9]+\.[0-9]+)(?:\s|$)", ssl.OPENSSL_VERSION)
    if not match:
        raise RuntimeError(f"Unsupported bundled TLS runtime: {ssl.OPENSSL_VERSION}")
    version = match[1]
    tag = f"openssl-{version}"
    url = f"https://raw.githubusercontent.com/openssl/openssl/{tag}/LICENSE.txt"
    license_text = download(url)
    if b"Apache License" not in license_text or b"Version 2.0" not in license_text:
        raise RuntimeError("Unexpected upstream OpenSSL license text")
    (destination / "openssl-LICENSE.txt").write_bytes(license_text)
    (destination / "runtime-NOTICES.txt").write_text(
        f"Python {sys.version.split()[0]} — Python Software Foundation and contributors.\n"
        "The included python-LICENSE.txt retains its upstream terms and copyright notices.\n"
        f"Source: https://github.com/python/cpython/tree/v{sys.version.split()[0]}\n\n"
        f"{ssl.OPENSSL_VERSION} — The OpenSSL Project Authors.\n"
        "Licensed under Apache License 2.0; see openssl-LICENSE.txt.\n"
        f"Source and upstream notices: https://github.com/openssl/openssl/tree/{tag}\n"
        f"License source: {url}\n\n"
        "PySide6/Qt 6.11.2 — The Qt Company and contributors.\n"
        "Selected Qt Widgets modules are dynamically linked under LGPLv3.\n"
        "See qt-qtbase-LGPL-3.0-only.txt and qt-qtbase-GPL-3.0-only.txt.\n"
        "Sources: https://github.com/pyside/pyside-setup/tree/v6.11.2\n"
        "         https://github.com/qt/qtbase/tree/v6.11.2\n",
        encoding="utf-8")
    return {"python": sys.version.split()[0], "openssl": version}


def provision():
    TOOLS.mkdir(parents=True, exist_ok=True)
    gallery = provision_gallery()
    yt_base = f"https://github.com/yt-dlp/yt-dlp/releases/download/{YT_DLP_VERSION}/"
    checksums = download(yt_base + "SHA2-256SUMS").decode()
    yt_hash = next(line.split()[0] for line in checksums.splitlines()
                   if line.split()[-1].lstrip("*") == "yt-dlp.exe")
    target = TOOLS / "yt-dlp.exe"
    if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != yt_hash:
        payload = download(yt_base + "yt-dlp.exe")
        verify(payload, yt_hash)
        target.write_bytes(payload)
    deno_zip = download(f"https://github.com/denoland/deno/releases/download/{DENO_VERSION}/"
                        "deno-x86_64-pc-windows-msvc.zip")
    verify(deno_zip, DENO_SHA256)
    with zipfile.ZipFile(io.BytesIO(deno_zip)) as archive:
        # Write only the expected executable; do not extract arbitrary paths.
        (TOOLS / "deno.exe").write_bytes(archive.read("deno.exe"))
    licenses = TOOLS / "licenses"
    licenses.mkdir(exist_ok=True)
    cache = ROOT / "build" / "tool-cache"
    cache.mkdir(parents=True, exist_ok=True)
    ffmpeg_archive = cache / f"ffmpeg-{FFMPEG_VERSION}-essentials.zip"
    if not ffmpeg_archive.is_file() or hashlib.sha256(ffmpeg_archive.read_bytes()).hexdigest() != FFMPEG_SHA256:
        payload = download(FFMPEG_URL)
        verify(payload, FFMPEG_SHA256)
        ffmpeg_archive.write_bytes(payload)
    prefix = f"ffmpeg-{FFMPEG_VERSION}-essentials_build/"
    with zipfile.ZipFile(ffmpeg_archive) as archive:
        for binary in ("ffmpeg.exe", "ffprobe.exe"):
            (TOOLS / binary).write_bytes(archive.read(prefix + "bin/" + binary))
        for name in archive.namelist():
            if name.startswith(prefix) and name.rsplit("/", 1)[-1].lower() in {"license", "license.txt", "readme.txt"}:
                (licenses / ("ffmpeg-" + Path(name).name)).write_bytes(archive.read(name))
    (licenses / "ffmpeg-SOURCE.txt").write_text(
        f"FFmpeg {FFMPEG_VERSION}, Gyan essentials Windows x64 build.\n"
        f"Binary: {FFMPEG_URL}\nSHA256: {FFMPEG_SHA256}\n"
        "Exact FFmpeg source: https://github.com/FFmpeg/FFmpeg/tree/946fcce07b\n"
        "Source archive: https://github.com/FFmpeg/FFmpeg/archive/946fcce07b.tar.gz\n"
        "Build configuration is printed by tools/ffmpeg.exe -version.\n"
        "Distributor and dependency build references: https://www.gyan.dev/ffmpeg/builds/\n",
        encoding="utf-8")
    for repository, tag, file in [
        ("yt-dlp/yt-dlp", YT_DLP_VERSION, "LICENSE"),
        ("yt-dlp/yt-dlp", YT_DLP_VERSION, "THIRD_PARTY_LICENSES.txt"),
        ("denoland/deno", DENO_VERSION, "LICENSE.md"),
        ("qt/qtbase", "v6.11.2", "LICENSES/LGPL-3.0-only.txt"),
        ("qt/qtbase", "v6.11.2", "LICENSES/GPL-3.0-only.txt"),
    ]:
        payload = download(f"https://raw.githubusercontent.com/{repository}/{tag}/{file}")
        (licenses / (repository.replace("/", "-") + "-" + Path(file).name)).write_bytes(payload)
    runtime_notices(licenses)
    # Preserve upstream source and license references alongside redistributed tools.
    (TOOLS / "versions.json").write_text(json.dumps({
        "yt_dlp": {"version": YT_DLP_VERSION, "sha256": yt_hash,
                   "source": f"https://github.com/yt-dlp/yt-dlp/tree/{YT_DLP_VERSION}"},
        "deno": {"version": DENO_VERSION, "archive_sha256": DENO_SHA256,
                 "source": f"https://github.com/denoland/deno/tree/{DENO_VERSION}"},
        "ffmpeg": {"version": FFMPEG_VERSION, "archive_sha256": FFMPEG_SHA256,
                   "binary_source": FFMPEG_URL,
                   "source": "https://github.com/FFmpeg/FFmpeg/tree/946fcce07b"},
        "gallery_dl": gallery,
    }, indent=2), encoding="utf-8")
    print(f"Verified yt-dlp {YT_DLP_VERSION}, gallery-dl {GALLERY_DL_VERSION}, Deno {DENO_VERSION}, FFmpeg/FFprobe {FFMPEG_VERSION}")


if __name__ == "__main__":
    provision()
