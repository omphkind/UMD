"""Fetch pinned, checksum-verified upstream tools into the project tools folder."""

import hashlib
import json
from pathlib import Path
import urllib.request
import zipfile
import io

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
YT_DLP_VERSION = "2026.08.19"
DENO_VERSION = "v2.9.7"
DENO_SHA256 = "a0c3101b4158d1dfb7d6a78a7bf0f3de80c96bb423c152beec8beb22786f2238"


def download(url):
    request = urllib.request.Request(url, headers={"User-Agent": "UMD-build/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def verify(payload, expected):
    if hashlib.sha256(payload).hexdigest() != expected.lower():
        raise RuntimeError("Official tool checksum verification failed")


def provision():
    TOOLS.mkdir(parents=True, exist_ok=True)
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
    for repository, tag, file in [
        ("yt-dlp/yt-dlp", YT_DLP_VERSION, "LICENSE"),
        ("yt-dlp/yt-dlp", YT_DLP_VERSION, "THIRD_PARTY_LICENSES.txt"),
        ("denoland/deno", DENO_VERSION, "LICENSE.md"),
    ]:
        payload = download(f"https://raw.githubusercontent.com/{repository}/{tag}/{file}")
        (licenses / (repository.replace("/", "-") + "-" + file)).write_bytes(payload)
    # Preserve upstream source and license references alongside redistributed tools.
    (TOOLS / "versions.json").write_text(json.dumps({
        "yt_dlp": {"version": YT_DLP_VERSION, "sha256": yt_hash,
                   "source": f"https://github.com/yt-dlp/yt-dlp/tree/{YT_DLP_VERSION}"},
        "deno": {"version": DENO_VERSION, "archive_sha256": DENO_SHA256,
                 "source": f"https://github.com/denoland/deno/tree/{DENO_VERSION}"},
    }, indent=2), encoding="utf-8")
    print(f"Verified yt-dlp {YT_DLP_VERSION} and Deno {DENO_VERSION}")


if __name__ == "__main__":
    provision()
