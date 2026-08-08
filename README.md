<div align="center">

# 📮 mailbridge

**The email MCP server — give Claude, Codex, Cursor or any AI agent a real mail client for iCloud, Outlook, Gmail, QQ, 163 and any IMAP mailbox.**

**English** | [简体中文](README.zh-CN.md)

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen)
![Protocol](https://img.shields.io/badge/protocol-MCP-8A2BE2)
![License](https://img.shields.io/badge/license-MIT-green)
![i18n](https://img.shields.io/badge/lang-EN%20%2F%20中文-orange)

</div>

Search, read, draft, send, file, and bulk-export mail across every mailbox you own — in conversation. Written in pure Python with **zero third-party dependencies**: no `pip install`, no build step. If a Python 3.9+ interpreter exists, it runs.

## Why

Most agent platforms' built-in connectors cover Gmail and Microsoft 365 work accounts. They do not cover personal iCloud, personal Outlook.com, QQ, 163, or your own IMAP server — and a search connector is not a mail client. mailbridge adds the missing operations: attachments, folders, real sending, bulk export. It speaks plain MCP over stdio, so the same server works in Claude, Codex, Cursor, Cline, Gemini CLI — anything that can launch an MCP server.

| Provider | Method | Credential |
|---|---|---|
| iCloud Mail | IMAP/SMTP | App-specific password |
| Gmail | IMAP/SMTP | App password |
| QQ / 163 / 126 | IMAP/SMTP | Authorization code (授权码) |
| Yahoo / Fastmail / custom | IMAP/SMTP | Provider app password |
| Outlook.com / Hotmail | Microsoft Graph | Free Entra app registration (OAuth) |
| Work / school Exchange | Microsoft Graph | Same registration + tenant consent |

## Quick start

```bash
git clone https://github.com/henry200803/mailbridge.git
python mailbridge/server/setup.py        # interactive wizard: add accounts, sign in, test
```

Then register the server with any MCP client:

```jsonc
// Claude Desktop (claude_desktop_config.json), Cursor, Cline, …
{
  "mcpServers": {
    "mailbridge": {
      "command": "python",
      "args": ["/path/to/mailbridge/server/server.py"]
    }
  }
}
```

```bash
# Claude Code
claude mcp add mailbridge -- python /path/to/mailbridge/server/server.py
```

```bash
# Codex CLI
codex mcp add mailbridge -- python /path/to/mailbridge/server/server.py
```

Cowork users: install the packaged `.zip` through the plugin installer instead — the bundled `.mcp.json` wires everything up.

## Microsoft accounts need a (free) client ID

Microsoft retired password-based IMAP/SMTP, so Outlook.com / Hotmail and work/school mailboxes connect through Microsoft Graph with OAuth — which requires an **Entra app registration**. Most personal users don't have one yet. It is free (no Azure subscription), takes about five minutes in a browser, and **one registration covers every Microsoft mailbox you own**.

Follow the step-by-step guide — **[Registering a Microsoft app for mailbridge](skills/mail-setup/references/azure-app-registration.md)** — then paste the resulting *Application (client) ID* into the setup wizard. The guide also covers tenant values, every common `AADSTS` error, and the university-mailbox consent problem. Your client ID stays in your local `accounts.json`; it is configuration for your own registration, and this repository ships none.

## Tools

| Tool | Purpose |
|---|---|
| `mail_list_accounts` | What mailboxes exist and whether they work |
| `mail_list_folders` | Folder tree with unread/total counts |
| `mail_search` | Filterable search across one or all accounts |
| `mail_get_message` | One message in full, HTML converted to text |
| `mail_get_thread` | The whole conversation |
| `mail_digest` | Cross-account unread review in one call |
| `mail_download_attachment` | Save an attachment to disk |
| `mail_export` | Bulk search results to CSV / JSON |
| `mail_draft` | Save a draft, threaded to a reply if asked |
| `mail_send` | Send immediately — requires `confirm=true` |
| `mail_mark` | Read/unread, flagged/unflagged, in bulk |
| `mail_move` | File messages into a folder |
| `mail_delete` | To Trash by default; permanent needs `confirm=true` |
| `mail_auth_status` | OAuth health for Microsoft accounts |

## Bilingual 🇬🇧🇨🇳

Tool descriptions, the setup wizard, and provider hints ship in English **and** Simplified Chinese. Switch any time:

```bash
python server/setup.py lang zh   # or: lang en
```

or set the `MAILBRIDGE_LANG=zh` environment variable in your MCP client config. English is the fallback for anything untranslated.

## Cloud & portable mode

Credentials normally live in `~/.mailbridge/` on the machine running the server. On ephemeral machines — cloud sandboxes, containers — ship them **inside the bundle** instead: put a `.mailbridge/` directory (the same `accounts.json` + `tokens/`) next to `server/`. Resolution order:

`MAILBRIDGE_HOME` env → `~/.mailbridge` (when configured) → bundled `.mailbridge/`

so a bundle with baked-in credentials still defers to the local config on your own machine.

> ⚠️ A bundle containing credentials **is a secret** — never publish or share it. If one leaks, app passwords and the Entra registration are individually revocable.

### Reuse every authorization on another computer

Run the script to create an AES-256-GCM encrypted bundle. The password is hidden and never enters shell history:

```bash
python -m pip install cryptography
python server/credential_bundle.py export mailbridge-credentials.mbvault
```

Copy the `.mbvault` file to the other computer and run:

```bash
python server/credential_bundle.py import mailbridge-credentials.mbvault
```

Existing credentials are overwritten only with `--replace`. Git ignores `.mbvault`; still keep it in private storage and transfer its password separately.

## Security

- Credentials live **only** in `~/.mailbridge/accounts.json` (mode 0600) on the machine running the server. Nothing is transmitted anywhere except to your mail provider.
- No password ever passes through the conversation transcript — the wizard reads it with hidden input.
- App passwords and the Entra registration are individually revocable: delete one and mailbridge is cut off instantly.
- Sending and permanent deletion are gated behind an explicit `confirm=true` argument.

## Known limits

- Graph (Outlook/Exchange) attachments are supported up to 150 MB via chunked upload sessions; the provider's total-message-size limit still applies, as it does on every IMAP provider (typically 20–50 MB per message).
- IMAP full-text search is server-side; some providers (notably QQ) silently return nothing for non-ASCII search terms — filter by sender or date instead.
- `mail_get_thread` on IMAP matches normalised subjects rather than true threading headers; Graph accounts use real conversation ids.
- Most universities block third-party apps until IT approves the Entra registration — that is tenant policy, not something mailbridge can route around.

## Layout & tests

```
server/          MCP server (stdio JSON-RPC), IMAP + Graph backends, OAuth, i18n
skills/          agent workflow guides: mailbox setup & everyday mail handling
tests/           offline suite — no network, no credentials
```

```bash
python tests/test_mailbridge.py
```

## Credits

Built by **Sibo Huang**. Developed with **Claude Opus 5** (initial implementation, in Cowork) and **Claude Fable 5** (live testing, bug fixes, bilingual i18n, release engineering).

## License

[MIT](LICENSE) © 2026 Sibo Huang
