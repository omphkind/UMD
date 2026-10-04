# UMD project rules

- The integration branch is `master`.
- Read `docs/RELEASES.md` before changing CI, versioning, or releases.
- `VERSION` contains the active target version, initially `0.1.0`.
- Every completed sprint is integrated into `master` and publishes a beta of
  that same target version: `vX.Y.Z-beta.N`.
- Keep `X.Y.Z` unchanged between sprints. Only an explicit stable-release
  command publishes `vX.Y.Z`.
- Advance `VERSION` only after the previous version's stable GitHub Release
  has been published successfully. Never reuse a published version or tag.
- After a stable release, its beta releases and tags may be removed by the
  release workflow's explicit cleanup option. Never remove another version's
  betas or a stable release/tag.
- Release artifacts must come from a successful Python build and carry the
  requested version. Do not publish placeholder packages or source snapshots
  as application builds.
- Telegram credentials are repository secrets `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_CHAT_ID`; never put their values into files or logs.
