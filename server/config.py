"""Account configuration for mailbridge.

Credentials live on the user's own machine in ~/.mailbridge/accounts.json
(mode 0600). They are never passed through the model context, never sent
anywhere except to the mail provider itself.

Config shape:

{
  "accounts": [
    {
      "name": "icloud",
      "kind": "imap",
      "provider": "icloud",
      "email": "you@icloud.com",
      "password": "abcd-efgh-ijkl-mnop"
    },
    {
      "name": "outlook",
      "kind": "graph",
      "email": "you@outlook.com",
      "client_id": "00000000-0000-0000-0000-000000000000",
      "tenant": "consumers"
    },
    {
      "name": "university",
      "kind": "graph",
      "email": "you@student.example.edu",
      "client_id": "00000000-0000-0000-0000-000000000000",
      "tenant": "organizations"
    }
  ],
  "default_account": "icloud"
}
"""

from __future__ import annotations

import json
import os
import stat
from typing import Any, Dict, List, Optional

# A `.mailbridge/` directory shipped inside the plugin bundle (next to
# server/). Used as a fallback on ephemeral machines — e.g. cloud sandboxes —
# where the home directory has no configuration.
_BUNDLED_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, ".mailbridge")
)


def resolve_config_dir(env: "Optional[str]" = None) -> str:
    """Pick the configuration directory.

    Priority: MAILBRIDGE_HOME env > ~/.mailbridge (when configured) >
    a bundled .mailbridge/ next to the plugin (when configured) >
    ~/.mailbridge default.
    """
    if env:
        return env
    home = os.path.join(os.path.expanduser("~"), ".mailbridge")
    if os.path.exists(os.path.join(home, "accounts.json")):
        return home
    if os.path.exists(os.path.join(_BUNDLED_DIR, "accounts.json")):
        return _BUNDLED_DIR
    return home


CONFIG_DIR = resolve_config_dir(os.environ.get("MAILBRIDGE_HOME"))
CONFIG_PATH = os.environ.get(
    "MAILBRIDGE_CONFIG", os.path.join(CONFIG_DIR, "accounts.json")
)
TOKEN_DIR = os.path.join(CONFIG_DIR, "tokens")


# IMAP/SMTP presets. `imap_host` implies port 993 with implicit TLS.
# `smtp_mode` is "starttls" (port 587) or "ssl" (port 465).
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "icloud": {
        "label": "iCloud Mail",
        "imap_host": "imap.mail.me.com",
        "imap_port": 993,
        "smtp_host": "smtp.mail.me.com",
        "smtp_port": 587,
        "smtp_mode": "starttls",
        "password_hint": (
            "iCloud requires an app-specific password. Create one at "
            "account.apple.com > Sign-In and Security > App-Specific Passwords. "
            "Your normal Apple ID password will not work."
        ),
    },
    "gmail": {
        "label": "Gmail",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_mode": "starttls",
        "password_hint": (
            "Gmail requires an App Password (2-Step Verification must be on): "
            "myaccount.google.com > Security > 2-Step Verification > App passwords."
        ),
    },
    "qq": {
        "label": "QQ Mail",
        "imap_host": "imap.qq.com",
        "imap_port": 993,
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "smtp_mode": "ssl",
        "needs_imap_id": True,
        "password_hint": (
            "QQ Mail requires an authorization code (授权码), not your QQ password. "
            "Get it in QQ Mail settings > 账户 > IMAP/SMTP服务 > 开启."
        ),
    },
    "163": {
        "label": "163 Mail",
        "imap_host": "imap.163.com",
        "imap_port": 993,
        "smtp_host": "smtp.163.com",
        "smtp_port": 465,
        "smtp_mode": "ssl",
        "needs_imap_id": True,
        "password_hint": (
            "163 Mail requires an authorization code (授权码), not your login password. "
            "Enable it in 设置 > POP3/SMTP/IMAP."
        ),
    },
    "126": {
        "label": "126 Mail",
        "imap_host": "imap.126.com",
        "imap_port": 993,
        "smtp_host": "smtp.126.com",
        "smtp_port": 465,
        "smtp_mode": "ssl",
        "needs_imap_id": True,
        "password_hint": "126 Mail requires an authorization code (授权码).",
    },
    "yahoo": {
        "label": "Yahoo Mail",
        "imap_host": "imap.mail.yahoo.com",
        "imap_port": 993,
        "smtp_host": "smtp.mail.yahoo.com",
        "smtp_port": 587,
        "smtp_mode": "starttls",
        "password_hint": "Yahoo requires an app password from account security settings.",
    },
    "fastmail": {
        "label": "Fastmail",
        "imap_host": "imap.fastmail.com",
        "imap_port": 993,
        "smtp_host": "smtp.fastmail.com",
        "smtp_port": 465,
        "smtp_mode": "ssl",
        "password_hint": "Fastmail requires an app password from Settings > Privacy & Security.",
    },
    "generic": {
        "label": "Custom IMAP/SMTP",
        "password_hint": "Use whatever password or app password your provider requires.",
    },
}

# Microsoft Graph scopes. offline_access is what gets us a refresh token so the
# user only authorises once.
GRAPH_SCOPES = [
    "offline_access",
    "User.Read",
    "Mail.ReadWrite",
]


class ConfigError(Exception):
    pass


class Account:
    def __init__(self, raw: Dict[str, Any]) -> None:
        self.raw = raw
        self.name: str = raw.get("name") or ""
        self.kind: str = (raw.get("kind") or "imap").lower()
        self.email: str = raw.get("email") or ""
        self.provider: str = (raw.get("provider") or "generic").lower()
        self.display_name: str = raw.get("display_name") or ""

        if not self.name:
            raise ConfigError("Every account needs a 'name'.")
        if not self.email:
            raise ConfigError(f"Account '{self.name}' is missing 'email'.")
        if self.kind not in ("imap", "graph"):
            raise ConfigError(
                f"Account '{self.name}' has unknown kind '{self.kind}' "
                "(expected 'imap' or 'graph')."
            )

    # ---- IMAP settings ---------------------------------------------------

    def _preset(self) -> Dict[str, Any]:
        return PROVIDERS.get(self.provider, PROVIDERS["generic"])

    @property
    def password(self) -> str:
        pw = self.raw.get("password")
        if not pw:
            env_key = self.raw.get("password_env")
            if env_key:
                pw = os.environ.get(env_key)
        if not pw:
            import i18n

            hint = i18n.provider_hint(
                self.provider, self._preset().get("password_hint", "")
            )
            raise ConfigError(f"Account '{self.name}' has no password. {hint}")
        return pw

    @property
    def imap_host(self) -> str:
        v = self.raw.get("imap_host") or self._preset().get("imap_host")
        if not v:
            raise ConfigError(
                f"Account '{self.name}' needs 'imap_host' (no preset for provider "
                f"'{self.provider}')."
            )
        return v

    @property
    def imap_port(self) -> int:
        return int(self.raw.get("imap_port") or self._preset().get("imap_port") or 993)

    @property
    def smtp_host(self) -> str:
        v = self.raw.get("smtp_host") or self._preset().get("smtp_host")
        if not v:
            raise ConfigError(f"Account '{self.name}' needs 'smtp_host'.")
        return v

    @property
    def smtp_port(self) -> int:
        return int(self.raw.get("smtp_port") or self._preset().get("smtp_port") or 587)

    @property
    def smtp_mode(self) -> str:
        return (
            self.raw.get("smtp_mode") or self._preset().get("smtp_mode") or "starttls"
        ).lower()

    @property
    def needs_imap_id(self) -> bool:
        if "needs_imap_id" in self.raw:
            return bool(self.raw["needs_imap_id"])
        return bool(self._preset().get("needs_imap_id"))

    @property
    def imap_user(self) -> str:
        return self.raw.get("imap_user") or self.email

    # ---- Graph settings --------------------------------------------------

    @property
    def client_id(self) -> str:
        v = self.raw.get("client_id") or os.environ.get("MAILBRIDGE_MS_CLIENT_ID")
        if not v:
            raise ConfigError(
                f"Account '{self.name}' needs 'client_id' — the Application (client) ID "
                "of your Microsoft Entra app registration. See the mail-setup skill for "
                "how to create one (it is free and takes about five minutes)."
            )
        return v

    @property
    def tenant(self) -> str:
        # 'consumers' = personal Microsoft accounts only
        # 'organizations' = work/school accounts only
        # 'common' = both
        # or a specific tenant GUID / domain
        return self.raw.get("tenant") or "common"

    @property
    def token_path(self) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.name)
        return os.path.join(TOKEN_DIR, f"{safe}.json")

    def summary(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "email": self.email,
            "kind": self.kind,
        }
        if self.kind == "imap":
            out["provider"] = self.provider
            try:
                out["imap_host"] = self.imap_host
            except ConfigError:
                out["imap_host"] = None
            out["has_password"] = bool(self.raw.get("password") or self.raw.get("password_env"))
        else:
            out["tenant"] = self.tenant
            out["has_client_id"] = bool(
                self.raw.get("client_id") or os.environ.get("MAILBRIDGE_MS_CLIENT_ID")
            )
            out["authorized"] = os.path.exists(self.token_path)
        return out


class Config:
    def __init__(self, path: str = CONFIG_PATH) -> None:
        self.path = path
        self.accounts: List[Account] = []
        self.default_account: Optional[str] = None
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            self.accounts = []
            self.default_account = None
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"{self.path} is not valid JSON ({exc}). Fix or delete the file and "
                "run the setup wizard again."
            ) from exc
        raw_accounts = data.get("accounts") or []
        self.accounts = [Account(a) for a in raw_accounts]
        names = [a.name for a in self.accounts]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ConfigError(f"Duplicate account names in config: {sorted(dupes)}")
        self.default_account = data.get("default_account") or (
            names[0] if names else None
        )

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        data = {
            "accounts": [a.raw for a in self.accounts],
            "default_account": self.default_account,
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass

    def get(self, name: Optional[str]) -> Account:
        if not self.accounts:
            raise ConfigError(
                f"No mail accounts are configured yet. Run the setup wizard:\n"
                f"    python3 <plugin>/server/setup.py\n"
                f"It writes {self.path} on this machine; credentials never leave it."
            )
        if not name:
            name = self.default_account
        for acct in self.accounts:
            if acct.name == name:
                return acct
        # Allow addressing by email too, it's a natural thing for a model to try.
        for acct in self.accounts:
            if acct.email.lower() == str(name).lower():
                return acct
        known = ", ".join(a.name for a in self.accounts)
        raise ConfigError(f"No account named '{name}'. Configured accounts: {known}")

    def resolve_many(self, names: Optional[List[str]]) -> List[Account]:
        if not names:
            return list(self.accounts)
        return [self.get(n) for n in names]
