# UMD project rules

- The integration and beta-development branch is `master`; `main` is the
  stable-release branch and remains the default branch.
- Read `docs/RELEASES.md` before changing CI, versioning, or releases.
- `VERSION` contains the active target version, initially `0.1.0`.
- Every completed sprint is integrated into `master` and publishes a beta of
  that same target version: `vX.Y.Z-beta.N`.
- Keep `X.Y.Z` unchanged between sprints. Only an explicit stable-release
  command publishes `vX.Y.Z`.
- Stable publication must use the latest successfully published beta of the
  current `master` commit. The manual release workflow merges that pinned
  commit into `main`, checks that the source tree matches the beta, and builds
  and publishes stable from the resulting `main` commit.
- Advance `VERSION` only after the previous version's stable GitHub Release
  has been published successfully. Never reuse a published version or tag.
- After a stable release, its beta releases and tags may be removed by the
  release workflow's explicit cleanup option. Never remove another version's
  betas or a stable release/tag.
- Release artifacts must come from a successful Python build and carry the
  requested version. Do not publish placeholder packages or source snapshots
  as application builds.
- Every beta and stable release must include a Windows x64 portable ZIP with
  `UMD/UMD.exe`, bundled yt-dlp, Deno and Chromium, a build manifest and SHA256
  checksums. Run application tests and packaged executable version, self-test
  and environment checks before publication.
- Release notes are short, specific feature/fix bullets in
  `release-notes/X.Y.Z.md` (maximum eight lines). Avoid raw generated commit logs.
- Do not change application code on `main` outside the explicit stable
  promotion. Reviewed workflow/documentation bootstrap updates may be mirrored
  to `main` so manual Actions remain available and GITHUB_TOKEN can merge.
- Telegram credentials are repository secrets `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_CHAT_ID`; never put their values into files or logs.
