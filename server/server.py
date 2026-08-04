#!/usr/bin/env python3
"""mailbridge — an MCP server that gives AI agents a real email client.

Backends:
  * IMAP/SMTP  — iCloud, Gmail, QQ, 163/126, Fastmail, Yahoo, any custom host
  * MS Graph   — personal Outlook.com/Hotmail and work/school Exchange Online

Credentials live only in ~/.mailbridge/accounts.json on the machine running
this server. Run `python3 setup.py` to configure accounts.
"""

from __future__ import annotations

import imaplib
import os
import re
import smtplib
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import i18n  # noqa: E402
from backend_graph import GraphBackend, GraphError  # noqa: E402
from backend_imap import ImapBackend, ImapError  # noqa: E402
from config import Config, ConfigError  # noqa: E402
from emailutil import format_size, truncate  # noqa: E402
from mcp_stdio import MCPServer, ToolError  # noqa: E402
from oauth_ms import AuthError, token_status  # noqa: E402

VERSION = "1.1.0"

# Every exception type a backend can plausibly raise. imaplib and smtplib
# exceptions are included because a server answering BAD surfaces as
# imaplib.IMAP4.error, which would otherwise escape as an opaque crash.
BACKEND_ERRORS = (
    ImapError,
    GraphError,
    AuthError,
    ConfigError,
    imaplib.IMAP4.error,
    smtplib.SMTPException,
    OSError,
)

INSTRUCTIONS = """\
mailbridge gives you full read/write access to the user's configured mailboxes.

Ids: IMAP message ids look like "INBOX|48213" (folder|uid). Graph ids are long
opaque strings. Ids come from mail_search — never invent one, and re-search if a
call reports an id as stale.

Working efficiently:
  * mail_search returns summaries only. Call mail_get_message for full bodies,
    and only for the messages that matter.
  * Start with mail_list_accounts if you do not know which accounts exist.
  * Omit `accounts` on mail_search to search every configured account at once.
  * For "what needs my attention", use mail_digest rather than many searches.

Before sending or permanently deleting anything, show the user exactly what you
are about to do and get their explicit go-ahead in conversation. Both tools also
require confirm=true.
"""

server = MCPServer("mailbridge", VERSION, INSTRUCTIONS)

_config: Optional[Config] = None
_backends: Dict[str, Any] = {}


def cfg() -> Config:
    global _config
    if _config is None:
        try:
            _config = Config()
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc
    return _config


def reload_config() -> None:
    global _config
    _config = None
    _backends.clear()


def backend_for(name: Optional[str]):
    try:
        account = cfg().get(name)
    except ConfigError as exc:
        raise ToolError(str(exc)) from exc
    if account.name not in _backends:
        _backends[account.name] = (
            GraphBackend(account) if account.kind == "graph" else ImapBackend(account)
        )
    return _backends[account.name]


def all_accounts(names: Optional[List[str]]):
    try:
        accounts = cfg().resolve_many(names)
    except ConfigError as exc:
        raise ToolError(str(exc)) from exc
    if not accounts:
        raise ToolError(
            "No accounts configured. Run: python3 <plugin>/server/setup.py"
        )
    return accounts


def wrap(fn, *args, **kwargs):
    """Turn backend exceptions into clean, model-readable tool errors."""
    try:
        return fn(*args, **kwargs)
    except BACKEND_ERRORS as exc:
        raise ToolError(str(exc)) from exc


# --------------------------------------------------------------------- schemas

ACCOUNT_PROP = {
    "type": "string",
    "description": "Account name from mail_list_accounts. Omit to use the default account.",
}
ACCOUNTS_PROP = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Account names to include. Omit to search every configured account.",
}
FOLDER_PROP = {
    "type": "string",
    "description": (
        "Folder to look in. Accepts a real folder name or one of the roles "
        "'inbox', 'sent', 'drafts', 'trash', 'junk', 'archive'. Defaults to inbox."
    ),
}
IDS_PROP = {
    "type": "array",
    "items": {"type": "string"},
    "minItems": 1,
    "description": "Message ids from mail_search results.",
}


# ----------------------------------------------------------------------- tools


@server.tool(
    "mail_list_accounts",
    "List every configured mailbox with its type and connection status. Call this "
    "first when you do not already know which accounts exist.",
    {"type": "object", "properties": {}, "additionalProperties": False},
    {"readOnlyHint": True, "openWorldHint": True},
)
def mail_list_accounts() -> Dict[str, Any]:
    reload_config()
    conf = cfg()
    out = []
    for account in conf.accounts:
        info = account.summary()
        if account.kind == "graph":
            try:
                info.update(token_status(account))
            except Exception as exc:  # noqa: BLE001
                info["auth_error"] = str(exc)
        out.append(info)
    if not out:
        return {
            "accounts": [],
            "hint": (
                "No accounts configured yet. On the machine running mailbridge, run "
                "`python3 <plugin>/server/setup.py` to add one. Credentials stay in "
                f"{conf.path} and never pass through this conversation."
            ),
        }
    return {"accounts": out, "default_account": conf.default_account}


@server.tool(
    "mail_list_folders",
    "List the folders in a mailbox, with unread and total counts where the provider "
    "reports them. Use this to find the exact name of a folder before moving mail.",
    {
        "type": "object",
        "properties": {
            "account": ACCOUNT_PROP,
            "with_counts": {
                "type": "boolean",
                "default": False,
                "description": "Also fetch per-folder unread/total counts (slower on IMAP).",
            },
        },
        "additionalProperties": False,
    },
    {"readOnlyHint": True, "openWorldHint": True},
)
def mail_list_folders(account: Optional[str] = None, with_counts: bool = False) -> Dict[str, Any]:
    backend = backend_for(account)
    folders = wrap(backend.list_folders, refresh=True)
    if with_counts and isinstance(backend, ImapBackend):
        enriched = []
        for folder in folders:
            status = wrap(backend.folder_status, folder["name"])
            enriched.append({**folder, **status})
        folders = enriched
    return {"account": backend.account.name, "folders": folders}


@server.tool(
    "mail_search",
    "Search mail across one or all accounts and return message summaries (no bodies). "
    "Combine filters freely: free-text `query`, `sender`, `subject`, date range, "
    "unread-only, attachments-only. Results are newest first. Use the returned `id` "
    "with mail_get_message to read a message in full.",
    {
        "type": "object",
        "properties": {
            "accounts": ACCOUNTS_PROP,
            "folder": FOLDER_PROP,
            "query": {
                "type": "string",
                "description": "Free-text search over the whole message (subject, body, headers).",
            },
            "sender": {"type": "string", "description": "Match the From address or name."},
            "recipient": {"type": "string", "description": "Match a To address."},
            "subject": {"type": "string", "description": "Match text in the subject line."},
            "since": {
                "type": "string",
                "description": "Only messages on or after this date (YYYY-MM-DD or ISO-8601).",
            },
            "before": {
                "type": "string",
                "description": "Only messages strictly before this date (YYYY-MM-DD or ISO-8601).",
            },
            "unread_only": {
                "type": "boolean", "default": False,
                "description": "Only unread messages.",
            },
            "flagged_only": {
                "type": "boolean", "default": False,
                "description": "Only flagged/starred messages.",
            },
            "has_attachment": {
                "type": "boolean", "default": False,
                "description": "Only messages with attachments.",
            },
            "limit": {
                "type": "integer",
                "default": 25,
                "minimum": 1,
                "maximum": 200,
                "description": "Max results per account.",
            },
        },
        "additionalProperties": False,
    },
    {"readOnlyHint": True, "openWorldHint": True},
)
def mail_search(
    accounts: Optional[List[str]] = None,
    folder: Optional[str] = None,
    query: Optional[str] = None,
    sender: Optional[str] = None,
    recipient: Optional[str] = None,
    subject: Optional[str] = None,
    since: Optional[str] = None,
    before: Optional[str] = None,
    unread_only: bool = False,
    flagged_only: bool = False,
    has_attachment: bool = False,
    limit: int = 25,
) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for account in all_accounts(accounts):
        backend = backend_for(account.name)
        try:
            found = backend.search(
                folder=folder,
                query=query,
                sender=sender,
                recipient=recipient,
                subject=subject,
                since=since,
                before=before,
                unread_only=unread_only,
                flagged_only=flagged_only,
                has_attachment=has_attachment,
                limit=limit,
            )
            results.extend(found)
        except BACKEND_ERRORS as exc:
            errors.append({"account": account.name, "error": str(exc)})

    results.sort(key=lambda m: m.get("date") or "", reverse=True)
    out: Dict[str, Any] = {"count": len(results), "messages": results}
    if errors:
        out["errors"] = errors
        if not results:
            raise ToolError(
                "Every account failed:\n"
                + "\n".join(f"- {e['account']}: {e['error']}" for e in errors)
            )
    return out


@server.tool(
    "mail_get_message",
    "Read one message in full: headers, decoded body text, and the list of "
    "attachments. HTML mail is converted to readable plain text.",
    {
        "type": "object",
        "properties": {
            "account": ACCOUNT_PROP,
            "message_id": {"type": "string", "description": "Id from mail_search."},
            "max_body_chars": {
                "type": "integer",
                "default": 8000,
                "minimum": 200,
                "maximum": 200000,
                "description": "Truncate the body at this many characters to protect context.",
            },
        },
        "required": ["message_id"],
        "additionalProperties": False,
    },
    {"readOnlyHint": True, "openWorldHint": True},
)
def mail_get_message(
    message_id: str, account: Optional[str] = None, max_body_chars: int = 8000
) -> Dict[str, Any]:
    backend = backend_for(account)
    msg = wrap(backend.get_message, message_id, include_body=True)
    body = msg.pop("body", "") or ""
    trimmed = truncate(body, max_body_chars)
    msg["body"] = trimmed["text"]
    msg["body_truncated"] = trimmed["truncated"]
    msg["body_total_chars"] = trimmed["total_chars"]
    for att in msg.get("attachments", []):
        att["size_human"] = format_size(att.get("bytes"))
    return msg


@server.tool(
    "mail_get_thread",
    "Fetch the whole conversation a message belongs to, oldest first. On Graph "
    "accounts this uses the real conversation id; on IMAP it matches the normalised "
    "subject across the inbox and sent folders.",
    {
        "type": "object",
        "properties": {
            "account": ACCOUNT_PROP,
            "message_id": {"type": "string", "description": "Any message in the thread."},
            "limit": {
                "type": "integer", "default": 30, "minimum": 1, "maximum": 100,
                "description": "Max messages to return from the thread.",
            },
        },
        "required": ["message_id"],
        "additionalProperties": False,
    },
    {"readOnlyHint": True, "openWorldHint": True},
)
def mail_get_thread(
    message_id: str, account: Optional[str] = None, limit: int = 30
) -> Dict[str, Any]:
    backend = backend_for(account)
    if isinstance(backend, GraphBackend):
        messages = wrap(backend.get_thread, message_id, limit)
        return {"count": len(messages), "messages": messages}

    anchor = wrap(backend.get_message, message_id, include_body=False)
    base_subject = _normalise_subject(anchor.get("subject", ""))
    if not base_subject:
        return {"count": 1, "messages": [anchor]}

    seen: Dict[str, Dict[str, Any]] = {}
    for folder in ("inbox", "sent"):
        try:
            for msg in backend.search(folder=folder, subject=base_subject, limit=limit):
                if _normalise_subject(msg.get("subject", "")) == base_subject:
                    seen[msg["id"]] = msg
        except BACKEND_ERRORS:
            continue
    seen[anchor["id"]] = anchor
    messages = sorted(seen.values(), key=lambda m: m.get("date") or "")
    return {"count": len(messages), "messages": messages[:limit]}


@server.tool(
    "mail_download_attachment",
    "Download one attachment to a local directory and return the saved path. Get the "
    "attachment id or filename from mail_get_message first.",
    {
        "type": "object",
        "properties": {
            "account": ACCOUNT_PROP,
            "message_id": {"type": "string"},
            "attachment": {
                "type": "string",
                "description": "The attachment's `id` or `filename` from mail_get_message.",
            },
            "dest_dir": {
                "type": "string",
                "description": "Directory to save into. Defaults to ~/Downloads/mailbridge.",
            },
        },
        "required": ["message_id", "attachment"],
        "additionalProperties": False,
    },
    {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
)
def mail_download_attachment(
    message_id: str,
    attachment: str,
    account: Optional[str] = None,
    dest_dir: Optional[str] = None,
) -> Dict[str, Any]:
    target = dest_dir or os.path.join(os.path.expanduser("~"), "Downloads", "mailbridge")
    backend = backend_for(account)
    result = wrap(backend.save_attachment, message_id, attachment, target)
    result["size_human"] = format_size(result.get("bytes"))
    return result


@server.tool(
    "mail_digest",
    "Summarise what is waiting across every account: unread counts per folder plus "
    "the most recent unread messages. Use this for 'what needs my attention' or a "
    "daily inbox review instead of issuing many separate searches.",
    {
        "type": "object",
        "properties": {
            "accounts": ACCOUNTS_PROP,
            "since": {
                "type": "string",
                "description": "Only count messages on or after this date (YYYY-MM-DD).",
            },
            "per_account_limit": {
                "type": "integer",
                "default": 15,
                "minimum": 1,
                "maximum": 60,
                "description": "How many unread message summaries to return per account.",
            },
        },
        "additionalProperties": False,
    },
    {"readOnlyHint": True, "openWorldHint": True},
)
def mail_digest(
    accounts: Optional[List[str]] = None,
    since: Optional[str] = None,
    per_account_limit: int = 15,
) -> Dict[str, Any]:
    report: List[Dict[str, Any]] = []
    for account in all_accounts(accounts):
        backend = backend_for(account.name)
        entry: Dict[str, Any] = {"account": account.name, "email": account.email}
        try:
            unread = backend.search(
                folder="inbox",
                unread_only=True,
                since=since,
                limit=per_account_limit,
            )
            entry["unread_shown"] = len(unread)
            entry["messages"] = unread
            if isinstance(backend, GraphBackend):
                folders = backend.list_folders(refresh=True)
                entry["unread_by_folder"] = {
                    f["name"]: f["unread"]
                    for f in folders
                    if f.get("unread")
                }
            else:
                status = backend.folder_status("INBOX")
                entry["inbox_unread"] = status.get("unread")
                entry["inbox_total"] = status.get("total")
        except BACKEND_ERRORS as exc:
            entry["error"] = str(exc)
        report.append(entry)
    return {"generated_for": [r["account"] for r in report], "accounts": report}


@server.tool(
    "mail_export",
    "Run a search and write the full result set to a CSV or JSON file on disk. Use "
    "this for bulk work — receipts for a tax year, every message from a sender, an "
    "attachment inventory — instead of pulling hundreds of messages into context.",
    {
        "type": "object",
        "properties": {
            "accounts": ACCOUNTS_PROP,
            "folder": FOLDER_PROP,
            "query": {"type": "string"},
            "sender": {"type": "string"},
            "subject": {"type": "string"},
            "since": {"type": "string"},
            "before": {"type": "string"},
            "has_attachment": {
                "type": "boolean", "default": False,
                "description": "Only messages with attachments.",
            },
            "limit": {
                "type": "integer",
                "default": 500,
                "minimum": 1,
                "maximum": 5000,
                "description": "Max messages per account.",
            },
            "out_path": {
                "type": "string",
                "description": "File to write. Extension decides the format (.csv or .json).",
            },
            "include_bodies": {
                "type": "boolean",
                "default": False,
                "description": "Also fetch and include each message body. Much slower.",
            },
        },
        "required": ["out_path"],
        "additionalProperties": False,
    },
    {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
)
def mail_export(
    out_path: str,
    accounts: Optional[List[str]] = None,
    folder: Optional[str] = None,
    query: Optional[str] = None,
    sender: Optional[str] = None,
    subject: Optional[str] = None,
    since: Optional[str] = None,
    before: Optional[str] = None,
    has_attachment: bool = False,
    limit: int = 500,
    include_bodies: bool = False,
) -> Dict[str, Any]:
    import csv
    import json as _json

    rows: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for account in all_accounts(accounts):
        backend = backend_for(account.name)
        try:
            found = backend.search(
                folder=folder,
                query=query,
                sender=sender,
                subject=subject,
                since=since,
                before=before,
                has_attachment=has_attachment,
                limit=limit,
            )
        except BACKEND_ERRORS as exc:
            errors.append({"account": account.name, "error": str(exc)})
            continue
        for msg in found:
            if include_bodies:
                try:
                    full = backend.get_message(msg["id"], include_body=True)
                    msg = {**msg, "body": full.get("body", "")}
                except BACKEND_ERRORS:
                    msg = {**msg, "body": ""}
            rows.append(msg)

    rows.sort(key=lambda m: m.get("date") or "", reverse=True)
    path = os.path.expanduser(out_path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    if path.lower().endswith(".json"):
        with open(path, "w", encoding="utf-8") as fh:
            _json.dump(rows, fh, ensure_ascii=False, indent=2, default=str)
    else:
        fields = [
            "date", "account", "folder", "from_name", "from_email", "to", "subject",
            "unread", "flagged", "has_attachments", "size", "id",
        ]
        if include_bodies:
            fields.append("body")
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for msg in rows:
                sender_obj = msg.get("from") or {}
                writer.writerow(
                    {
                        "date": msg.get("date"),
                        "account": msg.get("account"),
                        "folder": msg.get("folder"),
                        "from_name": sender_obj.get("name"),
                        "from_email": sender_obj.get("email"),
                        "to": "; ".join(a.get("email", "") for a in msg.get("to") or []),
                        "subject": msg.get("subject"),
                        "unread": msg.get("unread"),
                        "flagged": msg.get("flagged"),
                        "has_attachments": msg.get("has_attachments"),
                        "size": msg.get("size"),
                        "id": msg.get("id"),
                        "body": msg.get("body", ""),
                    }
                )

    out: Dict[str, Any] = {"written": path, "rows": len(rows)}
    if errors:
        out["errors"] = errors
    return out


@server.tool(
    "mail_draft",
    "Save a draft in the mailbox without sending it. This is the safe way to prepare "
    "a reply — the user opens their mail client, reads it, and sends it themselves. "
    "Pass reply_to_message_id to keep the draft threaded to an existing conversation.",
    {
        "type": "object",
        "properties": {
            "account": ACCOUNT_PROP,
            "to": {"type": "array", "items": {"type": "string"}, "description": "Recipient addresses."},
            "cc": {"type": "array", "items": {"type": "string"}, "description": "Cc addresses."},
            "bcc": {"type": "array", "items": {"type": "string"}, "description": "Bcc addresses."},
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "Plain text body."},
            "html_body": {"type": "string", "description": "Optional HTML body."},
            "reply_to_message_id": {
                "type": "string",
                "description": "Message id being replied to, to preserve threading.",
            },
            "attachments": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Local file paths to attach.",
            },
        },
        "required": ["subject", "body"],
        "additionalProperties": False,
    },
    {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
)
def mail_draft(
    subject: str,
    body: str,
    account: Optional[str] = None,
    to: Optional[List[str]] = None,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    html_body: Optional[str] = None,
    reply_to_message_id: Optional[str] = None,
    attachments: Optional[List[str]] = None,
) -> Dict[str, Any]:
    backend = backend_for(account)
    if isinstance(backend, GraphBackend):
        return wrap(
            backend.create_draft,
            to or [],
            subject,
            body,
            cc,
            bcc,
            html_body,
            reply_to_message_id,
            attachments,
        )

    in_reply_to = references = None
    if reply_to_message_id:
        original = wrap(backend.get_message, reply_to_message_id, include_body=False)
        in_reply_to = original.get("rfc_message_id")
        references = original.get("references")
        if not to:
            sender = original.get("from") or {}
            to = [sender.get("email")] if sender.get("email") else []
    if not to:
        raise ToolError("`to` is required (or pass reply_to_message_id to infer it).")

    msg = wrap(
        backend.build_message,
        to, subject, body, cc, bcc, html_body, attachments, in_reply_to, references,
    )
    return wrap(backend.append_draft, msg)


@server.tool(
    "mail_send",
    "Send an email immediately. Requires confirm=true. Show the user the exact "
    "recipients, subject and body and get their explicit approval in conversation "
    "before calling this — prefer mail_draft when there is any doubt.",
    {
        "type": "object",
        "properties": {
            "account": ACCOUNT_PROP,
            "to": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "cc": {"type": "array", "items": {"type": "string"}, "description": "Cc addresses."},
            "bcc": {"type": "array", "items": {"type": "string"}, "description": "Bcc addresses."},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "html_body": {"type": "string"},
            "reply_to_message_id": {"type": "string"},
            "attachments": {"type": "array", "items": {"type": "string"}},
            "confirm": {
                "type": "boolean",
                "description": "Must be true. Set it only after the user has approved this exact message.",
            },
        },
        "required": ["to", "subject", "body", "confirm"],
        "additionalProperties": False,
    },
    {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
)
def mail_send(
    to: List[str],
    subject: str,
    body: str,
    confirm: bool,
    account: Optional[str] = None,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    html_body: Optional[str] = None,
    reply_to_message_id: Optional[str] = None,
    attachments: Optional[List[str]] = None,
) -> Dict[str, Any]:
    if not confirm:
        raise ToolError(
            "Refusing to send with confirm=false. Show the user the recipients, "
            "subject and body, get their explicit approval, then call again with "
            "confirm=true — or use mail_draft instead."
        )
    backend = backend_for(account)
    if isinstance(backend, GraphBackend):
        return wrap(
            backend.send,
            to, subject, body, cc, bcc, html_body, reply_to_message_id, attachments,
        )

    in_reply_to = references = None
    if reply_to_message_id:
        original = wrap(backend.get_message, reply_to_message_id, include_body=False)
        in_reply_to = original.get("rfc_message_id")
        references = original.get("references")
    msg = wrap(
        backend.build_message,
        to, subject, body, cc, bcc, html_body, attachments, in_reply_to, references,
    )
    return wrap(backend.send, msg)


@server.tool(
    "mail_mark",
    "Mark messages read/unread and/or flagged/unflagged. Accepts many ids at once.",
    {
        "type": "object",
        "properties": {
            "account": ACCOUNT_PROP,
            "message_ids": IDS_PROP,
            "read": {"type": "boolean", "description": "true = mark read, false = mark unread."},
            "flagged": {"type": "boolean", "description": "true = flag/star, false = unflag."},
        },
        "required": ["message_ids"],
        "additionalProperties": False,
    },
    {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def mail_mark(
    message_ids: List[str],
    account: Optional[str] = None,
    read: Optional[bool] = None,
    flagged: Optional[bool] = None,
) -> Dict[str, Any]:
    if read is None and flagged is None:
        raise ToolError("Pass at least one of `read` or `flagged`.")
    backend = backend_for(account)
    return wrap(backend.set_flags, message_ids, read, flagged)


@server.tool(
    "mail_move",
    "Move messages to another folder. Use mail_list_folders first to get the exact "
    "destination name, or pass a role like 'archive' or 'trash'.",
    {
        "type": "object",
        "properties": {
            "account": ACCOUNT_PROP,
            "message_ids": IDS_PROP,
            "destination": {"type": "string", "description": "Target folder name or role."},
        },
        "required": ["message_ids", "destination"],
        "additionalProperties": False,
    },
    {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
)
def mail_move(
    message_ids: List[str], destination: str, account: Optional[str] = None
) -> Dict[str, Any]:
    backend = backend_for(account)
    return wrap(backend.move, message_ids, destination)


@server.tool(
    "mail_delete",
    "Delete messages. By default they go to Trash and can be recovered. "
    "permanent=true erases them irreversibly and additionally requires confirm=true — "
    "always list what will be deleted and get the user's explicit approval first.",
    {
        "type": "object",
        "properties": {
            "account": ACCOUNT_PROP,
            "message_ids": IDS_PROP,
            "permanent": {
                "type": "boolean",
                "default": False,
                "description": "Skip Trash and erase immediately. Irreversible.",
            },
            "confirm": {
                "type": "boolean",
                "default": False,
                "description": "Required when permanent=true.",
            },
        },
        "required": ["message_ids"],
        "additionalProperties": False,
    },
    {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
)
def mail_delete(
    message_ids: List[str],
    account: Optional[str] = None,
    permanent: bool = False,
    confirm: bool = False,
) -> Dict[str, Any]:
    if permanent and not confirm:
        raise ToolError(
            f"Refusing to permanently delete {len(message_ids)} message(s) without "
            "confirm=true. List them for the user, get explicit approval, then retry — "
            "or call again with permanent=false to move them to Trash instead."
        )
    backend = backend_for(account)
    return wrap(backend.delete, message_ids, permanent)


@server.tool(
    "mail_auth_status",
    "Check OAuth status for Microsoft accounts (personal Outlook.com and work/school "
    "Exchange). Reports whether each account is authorised and whether the cached "
    "token still refreshes. Tells the user exactly what to run if not.",
    {
        "type": "object",
        "properties": {"account": ACCOUNT_PROP},
        "additionalProperties": False,
    },
    {"readOnlyHint": True, "openWorldHint": False},
)
def mail_auth_status(account: Optional[str] = None) -> Dict[str, Any]:
    reload_config()
    conf = cfg()
    targets = [conf.get(account)] if account else conf.accounts
    out = []
    for acct in targets:
        if acct.kind != "graph":
            out.append({"account": acct.name, "kind": acct.kind, "oauth": "not applicable"})
            continue
        entry: Dict[str, Any] = {"account": acct.name, "email": acct.email, "tenant": acct.tenant}
        try:
            entry.update(token_status(acct))
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)
        if not entry.get("authorized"):
            entry["fix"] = (
                "On the machine running mailbridge, run: "
                f"python3 <plugin>/server/setup.py auth {acct.name}"
            )
        out.append(entry)
    return {"accounts": out}


# ---------------------------------------------------------------------- utils


_SUBJECT_PREFIX = re.compile(
    r"^\s*(?:(?:re|fw|fwd|aw|sv|vs|回复|回覆|答复|转发|轉發)\s*(?:\[\d+\])?\s*[:：]\s*)+",
    re.IGNORECASE,
)


def _normalise_subject(subject: str) -> str:
    previous = None
    current = subject or ""
    while previous != current:
        previous = current
        current = _SUBJECT_PREFIX.sub("", current).strip()
    return current


if __name__ == "__main__":
    i18n.localize_server(server)
    server.run()
