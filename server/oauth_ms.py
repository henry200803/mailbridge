"""Microsoft identity platform OAuth using the device code flow.

Device code flow is used deliberately: it needs no redirect URI, no client
secret, and no local web server, so the Entra app registration stays trivial
(one checkbox: "Allow public client flows"). It also works when the browser is
on a different machine than the server.

Tokens are cached at ~/.mailbridge/tokens/<account>.json with mode 0600.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional

from config import GRAPH_SCOPES, TOKEN_DIR, Account

AUTH_BASE = "https://login.microsoftonline.com"
# Refresh a bit early so a long tool call doesn't expire mid-flight.
EXPIRY_SKEW_SECONDS = 300


class AuthError(Exception):
    pass


def _post_form(url: str, fields: Dict[str, str]) -> Dict[str, Any]:
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    # Microsoft occasionally accepts the form but the client times out while
    # waiting for its response.  A short retry avoids making the user repeat a
    # complete browser sign-in for a transient network failure.
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                raise AuthError(f"HTTP {exc.code} from Microsoft: {raw[:500]}") from exc
        except (TimeoutError, socket.timeout) as exc:
            if attempt == 2:
                raise AuthError("Microsoft login endpoint timed out after retries.") from exc
            time.sleep(2 * (attempt + 1))
        except urllib.error.URLError as exc:
            raise AuthError(f"Could not reach Microsoft login endpoint: {exc.reason}") from exc


def _token_url(tenant: str) -> str:
    return f"{AUTH_BASE}/{tenant}/oauth2/v2.0/token"


def _device_code_url(tenant: str) -> str:
    return f"{AUTH_BASE}/{tenant}/oauth2/v2.0/devicecode"


# ---------------------------------------------------------------- token cache


def _read_cache(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        os.chmod(os.path.dirname(path), stat.S_IRWXU)
    except OSError:
        pass


def _store(account: Account, payload: Dict[str, Any]) -> Dict[str, Any]:
    if "access_token" not in payload:
        raise AuthError(_describe_error(payload))
    cached = {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token"),
        "expires_at": time.time() + int(payload.get("expires_in", 3600)),
        "scope": payload.get("scope", ""),
        "email": account.email,
        "tenant": account.tenant,
    }
    _write_cache(account.token_path, cached)
    return cached


def _describe_error(payload: Dict[str, Any]) -> str:
    code = payload.get("error", "unknown_error")
    desc = payload.get("error_description", "")
    hint = ""
    if code == "invalid_client" or "AADSTS7000218" in desc:
        hint = (
            "\nHint: in your Entra app registration, open Authentication and set "
            "'Allow public client flows' to Yes."
        )
    elif "AADSTS65001" in desc or code == "consent_required":
        hint = (
            "\nHint: this account's organisation has not consented to the app. "
            "A tenant admin must approve it (common at universities)."
        )
    elif "AADSTS50020" in desc:
        hint = (
            "\nHint: the account you signed in with does not exist in the tenant this "
            "account is configured for. Check the 'tenant' field: use 'consumers' for "
            "personal Outlook.com, 'organizations' for work/school."
        )
    elif "AADSTS900023" in desc or "AADSTS500011" in desc:
        hint = "\nHint: the 'tenant' value in your config is not a valid tenant."
    return f"Microsoft rejected the request ({code}): {desc}{hint}"


# ------------------------------------------------------------- public surface


def begin_device_flow(account: Account) -> Dict[str, Any]:
    """Start a device code flow. Returns the user_code / verification_uri to show."""
    payload = _post_form(
        _device_code_url(account.tenant),
        {"client_id": account.client_id, "scope": " ".join(GRAPH_SCOPES)},
    )
    if "device_code" not in payload:
        raise AuthError(_describe_error(payload))
    return payload


def poll_device_flow(
    account: Account,
    device: Dict[str, Any],
    on_wait: Optional[Callable[[int], None]] = None,
) -> Dict[str, Any]:
    """Poll until the user finishes signing in, then cache the tokens."""
    interval = int(device.get("interval", 5))
    deadline = time.time() + int(device.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        payload = _post_form(
            _token_url(account.tenant),
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": account.client_id,
                "device_code": device["device_code"],
            },
        )
        err = payload.get("error")
        if err is None:
            return _store(account, payload)
        if err == "authorization_pending":
            if on_wait:
                on_wait(interval)
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err in ("authorization_declined", "expired_token", "bad_verification_code"):
            raise AuthError(_describe_error(payload))
        raise AuthError(_describe_error(payload))
    raise AuthError("Device code expired before sign-in completed. Start over.")


def get_access_token(account: Account) -> str:
    """Return a valid access token, refreshing silently when possible."""
    cached = _read_cache(account.token_path)
    if not cached:
        raise AuthError(
            f"Account '{account.name}' has not been authorised yet. Run:\n"
            f"    python3 <plugin>/server/setup.py auth {account.name}\n"
            "on the machine where mailbridge is installed, then sign in with the code "
            "it prints. You only need to do this once."
        )

    if cached.get("expires_at", 0) - EXPIRY_SKEW_SECONDS > time.time():
        return cached["access_token"]

    refresh = cached.get("refresh_token")
    if not refresh:
        raise AuthError(
            f"The saved token for '{account.name}' has expired and there is no refresh "
            "token. Re-authorise with: python3 <plugin>/server/setup.py auth "
            f"{account.name}"
        )

    payload = _post_form(
        _token_url(cached.get("tenant") or account.tenant),
        {
            "grant_type": "refresh_token",
            "client_id": account.client_id,
            "refresh_token": refresh,
            "scope": " ".join(GRAPH_SCOPES),
        },
    )
    if "access_token" not in payload:
        raise AuthError(
            _describe_error(payload)
            + f"\nRe-authorise with: python3 <plugin>/server/setup.py auth {account.name}"
        )
    # Microsoft may or may not rotate the refresh token; keep the old one if not.
    if not payload.get("refresh_token"):
        payload["refresh_token"] = refresh
    return _store(account, payload)["access_token"]


def forget(account: Account) -> bool:
    if os.path.exists(account.token_path):
        os.remove(account.token_path)
        return True
    return False


def token_status(account: Account) -> Dict[str, Any]:
    cached = _read_cache(account.token_path)
    if not cached:
        return {"authorized": False}
    expires_at = cached.get("expires_at", 0)
    return {
        "authorized": True,
        "has_refresh_token": bool(cached.get("refresh_token")),
        "access_token_valid": expires_at - EXPIRY_SKEW_SECONDS > time.time(),
        "scopes": cached.get("scope", ""),
        "token_file": account.token_path,
    }


__all__ = [
    "AuthError",
    "begin_device_flow",
    "poll_device_flow",
    "get_access_token",
    "forget",
    "token_status",
    "TOKEN_DIR",
]
