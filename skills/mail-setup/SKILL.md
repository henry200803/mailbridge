---
name: mail-setup
description: Sets up or repairs a mailbox connection in mailbridge. Use when the user says "connect my email", "add my iCloud account", "link my Outlook", "hook up my Gmail", "my mailbox stopped working", "mail_search says not authorised", or when any mailbridge tool reports a missing account, missing password, missing client ID, or expired token.
---

# Connecting a mailbox to mailbridge

All configuration happens on the user's own machine via a setup wizard. Never ask
the user to paste a password or app password into the conversation — the wizard
reads it with hidden input and writes it to `~/.mailbridge/accounts.json` (mode
0600). Credentials must never enter the transcript.

## First: find out what is actually wrong

Call `mail_list_accounts`. It reports every configured account and, for Microsoft
accounts, whether OAuth has been completed. Match what it returns:

| Symptom | Cause | Fix |
|---|---|---|
| Empty account list | Nothing configured yet | Run the wizard (below) |
| `has_password: false` | IMAP account with no credential | Re-run `setup.py add`, or edit the config |
| `has_client_id: false` | Microsoft account, no app registration | `references/azure-app-registration.md` |
| `authorized: false` | Registered but never signed in | `python3 <plugin>/server/setup.py auth NAME` |
| Login failures on IMAP | Provider rejects the plain password | `references/app-passwords.md` |
| 403 from Graph on a work account | Tenant has not consented | Escalate to their IT — see below |

## Running the wizard

Tell the user to run this in a terminal on the machine where the plugin is
installed:

```
python3 ~/.claude/plugins/mailbridge/server/setup.py
```

If that path does not exist, have them locate the plugin directory and run
`server/setup.py` inside it. The wizard is menu-driven and self-explanatory; it
covers adding accounts, signing in to Microsoft, testing a connection, and
removing accounts.

Non-interactive shortcuts worth knowing:

```
python3 setup.py add            # add one mailbox
python3 setup.py list           # show what is configured
python3 setup.py auth NAME      # Microsoft device-code sign-in
python3 setup.py test NAME      # connect and report what works
```

Always finish by having them run `setup.py test NAME`, then call
`mail_list_accounts` yourself to confirm the server sees the same thing.

## Choosing the right account type

**iCloud, Gmail, QQ, 163, 126, Yahoo, Fastmail, anything self-hosted** — these
use IMAP with an app-specific password. Fast to set up, nothing to register.
See `references/app-passwords.md` for where each provider hides that setting.

**Outlook.com, Hotmail, Live, and any work or school Microsoft mailbox** —
these require OAuth. Microsoft retired password-based IMAP/POP/SMTP on consumer
mailboxes and on Exchange Online, so there is no app-password path here. The user
needs a free Entra app registration once; the same registration then serves every
Microsoft mailbox they own. Walk them through
`references/azure-app-registration.md`.

## Work and school accounts (universities, employers)

A university mailbox is Exchange Online behind the institution's Microsoft
tenant, so it uses the Graph backend with `"tenant": "organizations"`. The code
path is identical to personal Outlook.

The obstacle is policy, not protocol. Most institutions disable user consent for
unverified third-party apps, so sign-in fails with `AADSTS65001` or a
"needs admin approval" screen. That is the tenant admin's decision. When this
happens, be straight with the user: the app cannot route around it, and their
options are to ask IT to grant admin consent for the app registration, or to fall
back on forwarding to a mailbox they do control. Do not suggest workarounds that
amount to evading the institution's security policy.

## Adding a provider the wizard does not list

Choose "Other (custom IMAP/SMTP host)" and supply the hostnames. Or edit
`~/.mailbridge/accounts.json` directly — the shape is documented at the top of
`server/config.py`. Useful extra keys:

- `"needs_imap_id": true` — required by 163/126/QQ, which reject clients that do
  not send an IMAP `ID` command ("Unsafe Login" errors)
- `"password_env": "SOME_VAR"` — read the password from an environment variable
  instead of storing it in the file
- `"imap_user"` — when the login name differs from the email address
- `"display_name"` — the name outgoing mail is sent as

After any manual edit, call `mail_list_accounts` to force a config reload.
