# Third-party components

The portable distribution includes separate upstream components:

- yt-dlp Windows executable 2026.08.19: https://github.com/yt-dlp/yt-dlp/tree/2026.08.19
  The upstream executable combines components under GPLv3+; upstream notices
  and dependency licenses are preserved in `tools/licenses/`.
  Build/source information: https://github.com/yt-dlp/yt-dlp#compile
- Deno 2.9.7: https://github.com/denoland/deno/tree/v2.9.7 (MIT and dependency notices).
- gallery-dl Windows x64 nightly `1.33.0-dev:2026.10.05` (GPL-2.0):
  https://github.com/gdl-org/builds/releases/tag/2026.10.05
  SHA256: `a581635fc172f62e099e27ef742de985f363ebb06ea6fdaadd1db2594dd93e05`.
  Exact source: https://codeberg.org/mikf/gallery-dl/commit/b11951527bdb6f84e844075dca8332d920376236
  Its upstream GitHub README links these official builds after migration to Codeberg.
  GPL text and exact binary/source references are in `tools/licenses/`.
- FFmpeg/FFprobe 9.0.2 essentials Windows build by Gyan Doshi, a distributor
  linked by https://ffmpeg.org/download.html (GPLv3, static executable tools).
  Binary: https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip
  SHA256: `60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba`.
  Exact FFmpeg source: https://github.com/FFmpeg/FFmpeg/tree/946fcce07b
  Distributor README, build/source references and license are in `tools/licenses/`;
  `tools/ffmpeg.exe -version` displays the build configuration.
- PySide6/Qt 6.11.2: https://github.com/pyside/pyside-setup/tree/v6.11.2
  and https://github.com/qt/qtbase/tree/v6.11.2 (LGPLv3 for the Qt Widgets modules).
  LGPLv3 and GPLv3 texts are in `tools/licenses/`. Qt DLLs are separate files
  in `_internal/PySide6/`; compatible modified DLLs can replace them. UMD does
  not restrict reverse engineering for debugging changes to these libraries.
  Library licensing details: https://doc.qt.io/qtforpython-6/licenses.html
- Playwright 1.63.0: https://github.com/microsoft/playwright-python (Apache-2.0).
- Chromium and its third-party notices are included in the Playwright browser folder.
- Python: https://www.python.org/psf/license/
  The exact build interpreter's license/copyright text is preserved as
  `tools/licenses/python-LICENSE.txt`.
- OpenSSL: https://github.com/openssl/openssl (Apache-2.0).
  `tools/licenses/openssl-LICENSE.txt` comes from the tag matching the build
  interpreter's `ssl.OPENSSL_VERSION`. Its version and source references are
  recorded with Python/Qt attributions in `tools/licenses/runtime-NOTICES.txt`
  and the build manifest, so runner patch versions do not change the attribution.
- PyInstaller bootloader exception: https://pyinstaller.org/en/stable/license.html

Tool versions and checksums are recorded in `tools/versions.json`.
Runtime dependencies are not installed into the user's system.
