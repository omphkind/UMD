# Third-party components

The portable distribution includes separate upstream components:

- yt-dlp Windows executable 2026.08.19: https://github.com/yt-dlp/yt-dlp/tree/2026.08.19
  The upstream executable combines components under GPLv3+; upstream notices
  and dependency licenses are preserved in `tools/licenses/`.
  Build/source information: https://github.com/yt-dlp/yt-dlp#compile
- Deno 2.9.7: https://github.com/denoland/deno/tree/v2.9.7 (MIT and dependency notices).
- Playwright 1.63.0: https://github.com/microsoft/playwright-python (Apache-2.0).
- Chromium and its third-party notices are included in the Playwright browser folder.
- Python: https://www.python.org/psf/license/
- PyInstaller bootloader exception: https://pyinstaller.org/en/stable/license.html

Tool versions and checksums are recorded in `tools/versions.json`.
Runtime dependencies are not installed into the user's system.
