# Changelog

## 1.2.0 — 2026-08-04

- **New**: Microsoft Graph attachments over 3 MB now upload through chunked
  upload sessions (320 KiB-aligned chunks, per-chunk retry), raising the limit
  from 3 MB to Graph's 150 MB cap. IMAP providers were never affected.

## 1.1.0 — 2026-08-04

- **Bilingual**: tool descriptions, server instructions, setup wizard and
  provider hints now ship in English and Simplified Chinese. Switch with
  `setup.py lang zh|en` or `MAILBRIDGE_LANG`.
- **New**: portable bundled-credentials mode — a `.mailbridge/` directory next
  to `server/` is used automatically when `~/.mailbridge` is absent (cloud
  sandboxes, containers); `MAILBRIDGE_HOME` still overrides everything.
- **Fixed**: Graph `mail_get_thread` answered `400 InefficientFilter` — a
  `conversationId` filter cannot be combined with `$orderby`; sorting is now
  client-side.
- **Fixed**: IMAP `\NoSelect` / `\NonExistent` flag matching is now
  case-insensitive (QQ sends `\NoSelect`), so unselectable containers no
  longer leak into folder listings.
- New offline tests: 62 total.

## 1.0.2 — 2026-08-04

- **Fixed (P0)**: Graph folder listing requested the beta-only
  `wellKnownName` property and multi-level `$expand`, which the v1.0 endpoint
  rejects with 400 — breaking `mail_list_folders` and `mail_digest` for every
  Microsoft account. Roles are now resolved by probing each well-known folder
  id, and nesting is walked with explicit `childFolders` requests.

## 1.0.1 — 2026-08-03

- **Fixed**: reversible `mail_delete` silently escalated to a permanent
  expunge when no Trash folder was detected; it now refuses instead.
- **Fixed**: server stdio is forced to UTF-8 regardless of the launcher's
  locale (Windows ANSI code pages broke the wire format).
- **Fixed**: the offline test harness decoded subprocess pipes with the
  system locale and failed on GBK Windows.

## 1.0.0 — 2026-08-03

- Initial release: 14 MCP tools over IMAP/SMTP and Microsoft Graph.
