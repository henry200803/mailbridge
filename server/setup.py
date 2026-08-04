#!/usr/bin/env python3
"""mailbridge setup wizard.

Run this on the machine where mailbridge is installed. It writes
~/.mailbridge/accounts.json (mode 0600). Nothing you type here is sent
anywhere except to your mail provider.

Usage:
    python3 setup.py              interactive menu
    python3 setup.py add          add an account
    python3 setup.py list         show configured accounts
    python3 setup.py auth NAME    run Microsoft sign-in for a Graph account
    python3 setup.py test [NAME]  connect and report what works
    python3 setup.py remove NAME  delete an account from the config
    python3 setup.py lang [en|zh] show or set the interface language
"""

from __future__ import annotations

import getpass
import json
import os
import sys
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import i18n  # noqa: E402
import oauth_ms  # noqa: E402
from config import CONFIG_PATH, PROVIDERS, Account, Config, ConfigError  # noqa: E402
from i18n import t  # noqa: E402

BOLD = "\033[1m" if sys.stdout.isatty() else ""
DIM = "\033[2m" if sys.stdout.isatty() else ""
GREEN = "\033[32m" if sys.stdout.isatty() else ""
RED = "\033[31m" if sys.stdout.isatty() else ""
YELLOW = "\033[33m" if sys.stdout.isatty() else ""
OFF = "\033[0m" if sys.stdout.isatty() else ""


def say(msg: str = "") -> None:
    print(msg)


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def ask_secret(prompt: str) -> str:
    return getpass.getpass(f"{prompt}: ").strip()


def ask_yes(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    value = input(f"{prompt} [{hint}]: ").strip().lower()
    if not value:
        return default
    return value.startswith("y")


def _hint(provider: str) -> str:
    default = PROVIDERS.get(provider, PROVIDERS["generic"]).get("password_hint", "")
    return i18n.provider_hint(provider, default)


# ------------------------------------------------------------------ commands


PROVIDER_MENU = [
    ("icloud", "iCloud Mail", "imap"),
    ("outlook", "Outlook.com / Hotmail / Live (personal)", "graph"),
    ("work", "Work or school Microsoft account (Exchange Online, e.g. a university)", "graph"),
    ("gmail", "Gmail", "imap"),
    ("qq", "QQ Mail", "imap"),
    ("163", "163 Mail", "imap"),
    ("126", "126 Mail", "imap"),
    ("yahoo", "Yahoo Mail", "imap"),
    ("fastmail", "Fastmail", "imap"),
    ("generic", "Other (custom IMAP/SMTP host)", "imap"),
]

AZURE_HELP = """\
To reach a Microsoft mailbox you need a free app registration. This is a one-time,
five-minute job and it costs nothing.

  1. Go to  https://entra.microsoft.com  and sign in with ANY Microsoft account.
  2. Left sidebar: Applications > App registrations > "New registration".
  3. Name:  mailbridge   (anything is fine)
  4. Supported account types: choose
        "Accounts in any organizational directory and personal Microsoft accounts"
     This one option covers both Outlook.com and work/school mailboxes.
  5. Leave Redirect URI blank. Click Register.
  6. Copy the "Application (client) ID" from the overview page — that is the value
     you paste below.
  7. Left sidebar of your new app: Authentication. Scroll to "Advanced settings"
     and set "Allow public client flows" to  Yes.  Save.
  8. Left sidebar: API permissions > Add a permission > Microsoft Graph >
     Delegated permissions. Add:  Mail.ReadWrite,  Mail.Send,  User.Read,
     offline_access.  Then click "Add permissions".

Note for work/school accounts: many organisations (universities included) block
apps their IT has not approved. If sign-in later fails with a consent error, the
tenant admin has to approve it — that is a policy decision on their side, not
something the app can work around.
"""


def cmd_add(config: Config) -> None:
    say()
    say(f"{BOLD}{t('Add a mailbox')}{OFF}")
    for idx, (_, label, kind) in enumerate(PROVIDER_MENU, 1):
        tag = f"{DIM}(OAuth){OFF}" if kind == "graph" else ""
        say(f"  {idx:>2}. {t(label)} {tag}")
    say()
    while True:
        choice = ask(t("Which provider? (number)"))
        if choice.isdigit() and 1 <= int(choice) <= len(PROVIDER_MENU):
            break
        say(f"{RED}{t('Enter a number from the list.')}{OFF}")
    key, label, kind = PROVIDER_MENU[int(choice) - 1]

    email = ask(t("Email address"))
    if not email or "@" not in email:
        say(f"{RED}{t('That does not look like an email address.')}{OFF}")
        return
    default_name = key if key not in ("generic", "work") else email.split("@")[0]
    name = ask(t("Short name for this account (used in conversation)"), default_name)
    if any(a.name == name for a in config.accounts):
        msg = t("An account named '{name}' already exists.", name=name)
        say(f"{RED}{msg}{OFF}")
        return

    raw = {"name": name, "email": email, "kind": kind}
    display = ask(t("Display name to send mail as (optional)"), "")
    if display:
        raw["display_name"] = display

    if kind == "graph":
        raw["tenant"] = "consumers" if key == "outlook" else "organizations"
        say()
        say(i18n.azure_help(AZURE_HELP))
        client_id = ask(t("Application (client) ID"))
        if not client_id:
            say(f"{RED}{t('A client ID is required for Microsoft accounts.')}{OFF}")
            return
        raw["client_id"] = client_id
        if key == "work":
            tenant = ask(
                t(
                    "Tenant (press enter for 'organizations', or paste your tenant "
                    "domain/GUID if you know it)"
                ),
                "organizations",
            )
            raw["tenant"] = tenant
    else:
        raw["provider"] = key
        if key == "generic":
            raw["imap_host"] = ask(t("IMAP host (e.g. mail.example.com)"))
            raw["imap_port"] = int(ask(t("IMAP port"), "993"))
            raw["smtp_host"] = ask(t("SMTP host"), raw["imap_host"].replace("imap", "smtp"))
            raw["smtp_port"] = int(ask(t("SMTP port"), "587"))
            raw["smtp_mode"] = ask(t("SMTP mode (starttls or ssl)"), "starttls")
        say()
        say(f"{YELLOW}{_hint(key)}{OFF}")
        say()
        password = ask_secret(t("Password / app password (input is hidden)"))
        if not password:
            say(f"{RED}{t('A password is required.')}{OFF}")
            return
        raw["password"] = password

    config.accounts.append(Account(raw))
    if not config.default_account:
        config.default_account = name
    config.save()
    say()
    msg = t("Saved '{name}' to {path}", name=name, path=config.path)
    say(f"{GREEN}{msg}{OFF}")

    if kind == "graph":
        say()
        if ask_yes(t("Sign in to Microsoft now?")):
            do_auth(config, name)
    else:
        if ask_yes(t("Test the connection now?")):
            do_test(config, name)


def cmd_list(config: Config) -> None:
    if not config.accounts:
        say(t("No accounts configured yet. Run: python3 setup.py add"))
        return
    say()
    say(f"{BOLD}{t('Configured mailboxes')}{OFF}  {DIM}({config.path}){OFF}")
    for acct in config.accounts:
        marker = "*" if acct.name == config.default_account else " "
        extra = ""
        if acct.kind == "graph":
            status = oauth_ms.token_status(acct)
            extra = (
                f"{GREEN}{t('authorised')}{OFF}" if status.get("authorized")
                else f"{YELLOW}{t('not authorised — run: python3 setup.py auth {name}', name=acct.name)}{OFF}"
            )
        say(f" {marker} {acct.name:<14} {acct.email:<34} {acct.kind:<6} {extra}")
    say()
    say(f"{DIM}{t('* = default account')}{OFF}")


def do_auth(config: Config, name: str) -> None:
    try:
        acct = config.get(name)
    except ConfigError as exc:
        say(f"{RED}{exc}{OFF}")
        return
    if acct.kind != "graph":
        say(t("'{name}' is an IMAP account — it uses a password, not OAuth.", name=name))
        return
    try:
        device = oauth_ms.begin_device_flow(acct)
    except oauth_ms.AuthError as exc:
        say(f"{RED}{exc}{OFF}")
        return

    url = device.get("verification_uri") or "https://microsoft.com/devicelogin"
    code = device.get("user_code", "")
    say()
    say(f"{BOLD}{t('Sign in to Microsoft')}{OFF}")
    say(t("  1. Open  {url}", url=f"{BOLD}{url}{OFF}"))
    say(t("  2. Enter the code  {code}", code=f"{BOLD}{code}{OFF}"))
    say(t("  3. Sign in as  {email}  and approve the permissions.", email=acct.email))
    say()
    say(f"{DIM}{t('Waiting for you to finish… (Ctrl+C to cancel)')}{OFF}")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass

    try:
        oauth_ms.poll_device_flow(acct, device)
    except KeyboardInterrupt:
        say("\n" + t("Cancelled."))
        return
    except oauth_ms.AuthError as exc:
        say(f"{RED}{exc}{OFF}")
        return
    say(f"{GREEN}{t('Authorised. Token cached at {path}', path=acct.token_path)}{OFF}")
    do_test(config, name)


def do_test(config: Config, name: str) -> None:
    try:
        acct = config.get(name)
    except ConfigError as exc:
        say(f"{RED}{exc}{OFF}")
        return
    say()
    msg = t("Testing '{name}' ({email})", name=acct.name, email=acct.email)
    say(f"{BOLD}{msg}{OFF}")
    try:
        if acct.kind == "graph":
            from backend_graph import GraphBackend

            backend = GraphBackend(acct)
        else:
            from backend_imap import ImapBackend

            backend = ImapBackend(acct)

        folders = backend.list_folders(refresh=True)
        say(f"  {GREEN}{t('folders')}{OFF}   {t('{n} found', n=len(folders))}")
        roles = {f.get("role"): f["name"] for f in folders if f.get("role")}
        for role in ("inbox", "sent", "drafts", "trash"):
            found = roles.get(role)
            mark = f"{GREEN}ok{OFF}" if found else f"{YELLOW}{t('not detected')}{OFF}"
            say(f"  {role:<9} {mark} {DIM}{found or ''}{OFF}")

        recent = backend.search(folder="inbox", limit=3)
        say(f"  {GREEN}inbox{OFF}     {t('{n} recent message(s) readable', n=len(recent))}")
        for msg in recent:
            sender = (msg.get("from") or {}).get("email", "?")
            say(f"    {DIM}{(msg.get('date') or '')[:16]}  {sender[:32]:<32} "
                f"{(msg.get('subject') or '')[:44]}{OFF}")
        say()
        msg = t("'{name}' is working.", name=acct.name)
        say(f"{GREEN}{msg}{OFF}")
    except Exception as exc:  # noqa: BLE001
        say()
        say(f"{RED}{t('Failed:')}{OFF} {exc}")


def cmd_remove(config: Config, name: str) -> None:
    match = [a for a in config.accounts if a.name == name]
    if not match:
        msg = t("No account named '{name}'.", name=name)
        say(f"{RED}{msg}{OFF}")
        return
    if not ask_yes(t("Remove '{name}' from the config?", name=name), default=False):
        say(t("Cancelled."))
        return
    acct = match[0]
    config.accounts = [a for a in config.accounts if a.name != name]
    if config.default_account == name:
        config.default_account = config.accounts[0].name if config.accounts else None
    config.save()
    if acct.kind == "graph" and oauth_ms.forget(acct):
        say(f"{DIM}{t('Cached token deleted.')}{OFF}")
    msg = t("Removed '{name}'.", name=name)
    say(f"{GREEN}{msg}{OFF}")


def cmd_default(config: Config, name: str) -> None:
    if not any(a.name == name for a in config.accounts):
        msg = t("No account named '{name}'.", name=name)
        say(f"{RED}{msg}{OFF}")
        return
    config.default_account = name
    config.save()
    msg = t("Default account is now '{name}'.", name=name)
    say(f"{GREEN}{msg}{OFF}")


def cmd_lang(new_lang: str = "") -> None:
    """Show or persist the interface language ('en' or 'zh')."""
    if not new_lang:
        say(t("Current language: {lang}", lang=i18n.get_lang()))
        return
    value = new_lang.strip().lower()
    if value not in ("en", "zh"):
        msg = t("lang must be 'en' or 'zh'")
        say(f"{RED}{msg}{OFF}")
        return
    data = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            data = {}
    data["language"] = value
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    i18n.reset_lang()
    say(f"{GREEN}{t('Language saved: {lang}', lang=value)}{OFF}")
    say(t("Restart your MCP client for tool descriptions to switch."))


def interactive(config: Config) -> None:
    while True:
        say()
        say(f"{BOLD}{t('mailbridge setup')}{OFF}")
        cmd_list(config)
        say()
        say(f"  1. {t('Add a mailbox')}")
        say(f"  2. {t('Sign in to a Microsoft account')}")
        say(f"  3. {t('Test a mailbox')}")
        say(f"  4. {t('Set the default mailbox')}")
        say(f"  5. {t('Remove a mailbox')}")
        say(f"  6. {t('Language / 语言')}")
        say(f"  7. {t('Quit')}")
        choice = ask(t("Choose"), "7")
        if choice == "1":
            cmd_add(config)
        elif choice == "2":
            do_auth(config, ask(t("Account name")))
        elif choice == "3":
            target = ask(t("Account name (blank for all)"))
            targets = [target] if target else [a.name for a in config.accounts]
            for one in targets:
                do_test(config, one)
        elif choice == "4":
            cmd_default(config, ask(t("Account name")))
        elif choice == "5":
            cmd_remove(config, ask(t("Account name")))
        elif choice == "6":
            cmd_lang(ask(t("Switch to (en/zh)"), i18n.get_lang()))
        else:
            return
        config.load()


def main() -> int:
    try:
        config = Config()
    except ConfigError as exc:
        say(f"{RED}{exc}{OFF}")
        return 1

    args = sys.argv[1:]
    if not args:
        try:
            interactive(config)
        except (KeyboardInterrupt, EOFError):
            say("\n" + t("Bye."))
        return 0

    command = args[0].lower()
    try:
        if command == "add":
            cmd_add(config)
        elif command == "list":
            cmd_list(config)
        elif command == "auth":
            if len(args) < 2:
                say("Usage: python3 setup.py auth ACCOUNT_NAME")
                return 1
            do_auth(config, args[1])
        elif command == "test":
            targets = [args[1]] if len(args) > 1 else [a.name for a in config.accounts]
            if not targets:
                say(t("No accounts configured."))
            for name in targets:
                do_test(config, name)
        elif command == "remove":
            if len(args) < 2:
                say("Usage: python3 setup.py remove ACCOUNT_NAME")
                return 1
            cmd_remove(config, args[1])
        elif command == "default":
            if len(args) < 2:
                say("Usage: python3 setup.py default ACCOUNT_NAME")
                return 1
            cmd_default(config, args[1])
        elif command == "lang":
            cmd_lang(args[1] if len(args) > 1 else "")
        elif command in ("help", "-h", "--help"):
            say(__doc__)
        else:
            say(f"Unknown command '{command}'. Run: python3 setup.py help")
            return 1
    except (KeyboardInterrupt, EOFError):
        say("\n" + t("Cancelled."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
