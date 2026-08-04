"""IMAP/SMTP backend — covers iCloud, Gmail, QQ, 163, Fastmail, Yahoo and any
other provider that still accepts password (or app-password) authentication.

Message ids are "<folder>|<uid>", e.g. "INBOX|48213". UIDs are stable per
folder, so an id stays valid until the message is moved or deleted.
"""

from __future__ import annotations

import email
import imaplib
import os
import re
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import imap_utf7
from config import Account, ConfigError
from emailutil import (
    decode_header_value,
    html_to_text,
    iso_to_datetime,
    make_snippet,
    parse_address,
    parse_address_list,
    parse_date,
    strip_quoted_reply,
)

imaplib._MAXLINE = 10_000_000  # some providers send very long literal lines

# imaplib does not know the ID command (RFC 2971), so _simple_command('ID', ...)
# raises KeyError before writing anything. 163/126/QQ refuse to authenticate
# clients that skip it ("Unsafe Login"), so register it here.
imaplib.Commands.setdefault("ID", ("NONAUTH", "AUTH", "SELECTED"))

SUMMARY_HEADERS = (
    "FROM TO CC BCC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES LIST-ID"
)

# Special-use flags (RFC 6154) mapped to the roles we care about.
ROLE_BY_FLAG = {
    "\\All": "all",
    "\\Archive": "archive",
    "\\Drafts": "drafts",
    "\\Flagged": "flagged",
    "\\Junk": "junk",
    "\\Sent": "sent",
    "\\Trash": "trash",
}

# Fallback name matching for servers that don't advertise SPECIAL-USE.
ROLE_BY_NAME = {
    "drafts": "drafts", "draft": "drafts", "草稿箱": "drafts", "草稿": "drafts",
    "sent": "sent", "sent items": "sent", "sent messages": "sent",
    "已发送": "sent", "已寄郵件": "sent", "已發送": "sent",
    "trash": "trash", "deleted items": "trash", "deleted messages": "trash",
    "已删除": "trash", "已刪除": "trash", "垃圾桶": "trash",
    "junk": "junk", "spam": "junk", "junk e-mail": "junk", "垃圾邮件": "junk",
    "archive": "archive", "归档": "archive",
}


class ImapError(Exception):
    pass


def _q(value: str) -> bytes:
    """Quote a string for an IMAP command, UTF-8 encoded."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return b'"' + escaped.encode("utf-8") + b'"'


def _mbox(name: str) -> bytes:
    """Encode a mailbox name and quote it.

    imaplib does not quote command arguments, so an unquoted folder name with a
    space in it ("Sent Messages" on iCloud, "[Gmail]/Sent Mail") arrives at the
    server as two atoms and is rejected with BAD. Quoting is always valid, so we
    quote unconditionally.
    """
    encoded = imap_utf7.encode(name).replace(b"\\", b"\\\\").replace(b'"', b'\\"')
    return b'"' + encoded + b'"'


def _mbox_str(name: str) -> str:
    """Same as _mbox but as str, for commands built through imaplib's uid()."""
    return _mbox(name).decode("ascii")


def _imap_date(dt: datetime) -> str:
    return dt.strftime("%d-%b-%Y")


class ImapBackend:
    """One backend instance per account. Connections are cached and revalidated."""

    def __init__(self, account: Account) -> None:
        self.account = account
        self._conn: Optional[imaplib.IMAP4_SSL] = None
        self._selected: Optional[str] = None
        self._selected_readonly = True
        self._folder_cache: Optional[List[Dict[str, Any]]] = None

    # ------------------------------------------------------------ connection

    def _connect(self) -> imaplib.IMAP4_SSL:
        acct = self.account
        context = ssl.create_default_context()
        try:
            conn = imaplib.IMAP4_SSL(acct.imap_host, acct.imap_port, ssl_context=context)
        except (OSError, ssl.SSLError) as exc:
            raise ImapError(
                f"Could not reach {acct.imap_host}:{acct.imap_port} for account "
                f"'{acct.name}': {exc}"
            ) from exc

        # 163/126/QQ reject logins from clients that don't send an ID command.
        if acct.needs_imap_id:
            try:
                conn._simple_command(
                    "ID",
                    '("name" "mailbridge" "version" "1.0" '
                    '"vendor" "mailbridge" "support-email" "n/a")',
                )
                conn._untagged_response("OK", [None], "ID")
            except Exception as exc:  # noqa: BLE001 - ID is advisory, keep going
                sys.stderr.write(f"[mailbridge] IMAP ID command failed: {exc}\n")

        try:
            conn.login(acct.imap_user, acct.password)
        except ConfigError:
            raise
        except imaplib.IMAP4.error as exc:
            detail = str(exc)
            hint = ""
            low = detail.lower()
            if "authenticationfailed" in low.replace(" ", "") or "invalid" in low:
                hint = (
                    "\nMost providers reject your normal account password here — you "
                    "need an app-specific password / 授权码. "
                    f"{_password_hint(acct)}"
                )
            elif "unsafe" in low or "login fail" in low:
                hint = (
                    "\nThis provider requires the IMAP ID command. Add "
                    '"needs_imap_id": true to this account in accounts.json.'
                )
            raise ImapError(f"Login failed for '{acct.name}': {detail}{hint}") from exc
        return conn

    def conn(self) -> imaplib.IMAP4_SSL:
        if self._conn is not None:
            try:
                status, _ = self._conn.noop()
                if status == "OK":
                    return self._conn
            except Exception:  # noqa: BLE001 - stale socket, reconnect below
                pass
            self._reset()
        self._conn = self._connect()
        return self._conn

    def _reset(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:  # noqa: BLE001
                pass
        self._conn = None
        self._selected = None

    def close(self) -> None:
        self._reset()

    def _has_capability(self, name: str) -> bool:
        conn = self.conn()
        caps = getattr(conn, "capabilities", ()) or ()
        return name.upper() in {c.upper() for c in caps}

    def _expunge(self, uids: List[int]) -> None:
        """Expunge only the given UIDs.

        A bare EXPUNGE removes every \\Deleted message in the folder, including
        ones another client marked but has not yet expunged. That would be
        silent, irreversible data loss on a tool whose safety story is
        'deletes go to Trash first', so prefer UIDPLUS's UID EXPUNGE and only
        fall back when the server does not advertise it.
        """
        conn = self.conn()
        spec = ",".join(str(u) for u in uids)
        if self._has_capability("UIDPLUS"):
            try:
                status, _ = conn.uid("EXPUNGE", spec)
                if status == "OK":
                    return
            except imaplib.IMAP4.error:
                pass
        conn.expunge()

    # --------------------------------------------------------------- folders

    def _select(self, folder: str, readonly: bool = True) -> None:
        conn = self.conn()
        if self._selected == folder and self._selected_readonly == readonly:
            return
        try:
            status, data = conn.select(_mbox(folder), readonly=readonly)
        except imaplib.IMAP4.error as exc:
            status, data = "NO", [str(exc).encode()]
        if status != "OK":
            detail = _first(data)
            raise ImapError(
                f"Cannot open folder '{folder}' on account '{self.account.name}': "
                f"{detail}. Use mail_list_folders to see the exact folder names."
            )
        self._selected = folder
        self._selected_readonly = readonly

    def list_folders(self, refresh: bool = False) -> List[Dict[str, Any]]:
        if self._folder_cache is not None and not refresh:
            return self._folder_cache
        conn = self.conn()
        status, data = conn.list()
        if status != "OK":
            raise ImapError(f"LIST failed on '{self.account.name}': {_first(data)}")

        folders: List[Dict[str, Any]] = []
        for raw in data:
            parsed = _parse_list_line(raw)
            if parsed is None:
                continue
            flags, name = parsed
            # RFC 3501 flags are case-insensitive; QQ sends \NoSelect.
            lowered = {f.lower() for f in flags}
            if "\\noselect" in lowered or "\\nonexistent" in lowered:
                continue
            role = None
            from_flag = False
            for flag in flags:
                if flag in ROLE_BY_FLAG:
                    role = ROLE_BY_FLAG[flag]
                    from_flag = True
                    break
            if role is None:
                leaf = name.split("/")[-1].split(".")[-1].strip().lower()
                role = ROLE_BY_NAME.get(leaf)
            if name.upper() == "INBOX":
                role, from_flag = "inbox", True
            folders.append(
                {"name": name, "role": role, "flags": flags, "role_from_flag": from_flag}
            )
        self._folder_cache = folders
        return folders

    def folder_for_role(self, role: str) -> Optional[str]:
        """A folder that advertises SPECIAL-USE always beats a name-only guess —
        otherwise a localised alias like 已发送 can shadow the real Sent folder."""
        fallback: Optional[str] = None
        for folder in self.list_folders():
            if folder.get("role") != role:
                continue
            if folder.get("role_from_flag"):
                return folder["name"]
            if fallback is None:
                fallback = folder["name"]
        return fallback

    def folder_status(self, folder: str) -> Dict[str, Any]:
        conn = self.conn()
        try:
            status, data = conn.status(_mbox(folder), "(MESSAGES UNSEEN RECENT)")
        except imaplib.IMAP4.error:
            status, data = "NO", []
        if status != "OK":
            return {"name": folder, "total": None, "unread": None}
        text = _first(data)
        return {
            "name": folder,
            "total": _int_field(text, "MESSAGES"),
            "unread": _int_field(text, "UNSEEN"),
        }

    def _resolve_folder(self, folder: Optional[str]) -> str:
        if not folder or folder.upper() in ("INBOX", "收件箱"):
            return "INBOX"
        role = folder.strip().lower()
        if role in ("sent", "drafts", "trash", "junk", "spam", "archive"):
            if role == "spam":
                role = "junk"
            found = self.folder_for_role(role)
            if found:
                return found
        names = [f["name"] for f in self.list_folders()]
        if folder in names:
            return folder
        for name in names:  # case-insensitive match
            if name.lower() == folder.lower():
                return name
        for name in names:  # leaf match, e.g. "Receipts" for "INBOX/Receipts"
            if name.split("/")[-1].lower() == folder.lower():
                return name
        raise ImapError(
            f"Folder '{folder}' not found on '{self.account.name}'. "
            f"Available: {', '.join(names[:40])}"
        )

    def searchable_folders(self) -> List[str]:
        out = []
        for folder in self.list_folders():
            if folder.get("role") in ("trash", "junk"):
                continue
            if folder.get("role") == "all":
                continue  # Gmail's "All Mail" duplicates everything
            out.append(folder["name"])
        return out

    # ---------------------------------------------------------------- search

    def search(
        self,
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
    ) -> List[Dict[str, Any]]:
        target = self._resolve_folder(folder)
        self._select(target, readonly=True)

        criteria: List[bytes] = []
        if unread_only:
            criteria.append(b"UNSEEN")
        if flagged_only:
            criteria.append(b"FLAGGED")
        if sender:
            criteria.extend([b"FROM", _q(sender)])
        if recipient:
            criteria.extend([b"TO", _q(recipient)])
        if subject:
            criteria.extend([b"SUBJECT", _q(subject)])
        if query:
            criteria.extend([b"TEXT", _q(query)])
        since_dt = iso_to_datetime(since)
        if since_dt:
            criteria.extend([b"SINCE", _imap_date(since_dt).encode()])
        before_dt = iso_to_datetime(before)
        if before_dt:
            criteria.extend([b"BEFORE", _imap_date(before_dt).encode()])
        if not criteria:
            criteria.append(b"ALL")

        uids = self._uid_search(criteria)
        if not uids:
            return []

        # Highest UID ~= newest. Over-fetch when we still have to filter by
        # attachment presence.
        window = limit * 4 if has_attachment else limit
        selected = uids[-max(window, limit):]
        messages = self._fetch_summaries(target, selected)
        if has_attachment:
            messages = [m for m in messages if m.get("has_attachments")]
        messages.sort(key=lambda m: m.get("date") or "", reverse=True)
        return messages[:limit]

    def _uid_search(self, criteria: List[bytes]) -> List[int]:
        conn = self.conn()
        try:
            status, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *criteria)
        except imaplib.IMAP4.error:
            status = "NO"
            data = []
        if status != "OK":
            # Some servers reject the CHARSET argument; retry without it.
            try:
                status, data = conn.uid("SEARCH", None, *criteria)
            except imaplib.IMAP4.error as exc:
                raise ImapError(f"SEARCH failed: {exc}") from exc
        if status != "OK":
            raise ImapError(f"SEARCH failed: {_first(data)}")
        raw = _first(data)
        return [int(tok) for tok in raw.split() if tok.isdigit()]

    def _fetch_summaries(self, folder: str, uids: Iterable[int]) -> List[Dict[str, Any]]:
        uid_list = list(uids)
        if not uid_list:
            return []
        conn = self.conn()
        out: List[Dict[str, Any]] = []
        for batch in _chunks(uid_list, 100):
            spec = ",".join(str(u) for u in batch)
            status, data = conn.uid(
                "FETCH",
                spec,
                f"(UID FLAGS RFC822.SIZE INTERNALDATE BODYSTRUCTURE "
                f"BODY.PEEK[HEADER.FIELDS ({SUMMARY_HEADERS})])",
            )
            if status != "OK":
                raise ImapError(f"FETCH failed: {_first(data)}")
            out.extend(_parse_fetch_summaries(data, folder, self.account.name))
        return out

    # ------------------------------------------------------------- read one

    def get_message(
        self, message_id: str, include_body: bool = True, max_body_chars: int = 8000
    ) -> Dict[str, Any]:
        folder, uid = split_id(message_id)
        folder = self._resolve_folder(folder)
        self._select(folder, readonly=True)
        conn = self.conn()
        item = "(UID FLAGS RFC822.SIZE INTERNALDATE BODY.PEEK[])" if include_body else (
            f"(UID FLAGS RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER.FIELDS ({SUMMARY_HEADERS})])"
        )
        status, data = conn.uid("FETCH", str(uid), item)
        if status != "OK" or not data or data[0] is None:
            raise ImapError(
                f"Message {message_id} not found. It may have been moved or deleted; "
                "search again to get a current id."
            )
        raw = _extract_literal(data)
        if raw is None:
            raise ImapError(f"Could not read message body for {message_id}.")
        msg = email.message_from_bytes(raw)
        flags = _flags_from(data)
        result = _message_from_email(msg, folder, uid, self.account.name, flags)
        result["size"] = len(raw)
        if include_body:
            body = _extract_body(msg)
            result.update(body)
        return result

    def list_attachments(self, message_id: str) -> List[Dict[str, Any]]:
        return self.get_message(message_id, include_body=True).get("attachments", [])

    def save_attachment(
        self, message_id: str, attachment_id: str, dest_dir: str
    ) -> Dict[str, Any]:
        folder, uid = split_id(message_id)
        folder = self._resolve_folder(folder)
        self._select(folder, readonly=True)
        conn = self.conn()
        status, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if status != "OK":
            raise ImapError(f"Could not fetch {message_id}: {_first(data)}")
        raw = _extract_literal(data)
        if raw is None:
            raise ImapError(f"Could not fetch {message_id}.")
        msg = email.message_from_bytes(raw)

        for index, part in enumerate(_iter_attachment_parts(msg)):
            ident = str(index)
            filename = decode_header_value(part.get_filename() or "") or f"part-{index}"
            if attachment_id not in (ident, filename):
                continue
            payload = part.get_payload(decode=True) or b""
            os.makedirs(dest_dir, exist_ok=True)
            safe = _safe_filename(filename)
            path = _unique_path(os.path.join(dest_dir, safe))
            with open(path, "wb") as fh:
                fh.write(payload)
            return {
                "saved_to": path,
                "filename": safe,
                "bytes": len(payload),
                "content_type": part.get_content_type(),
            }
        raise ImapError(
            f"No attachment '{attachment_id}' on {message_id}. Call mail_get_message "
            "first and use the 'id' or 'filename' from its attachments list."
        )

    # ------------------------------------------------------------- mutations

    def set_flags(
        self,
        message_ids: List[str],
        read: Optional[bool] = None,
        flagged: Optional[bool] = None,
    ) -> Dict[str, Any]:
        changed = 0
        for folder, uids in _group_by_folder(message_ids).items():
            resolved = self._resolve_folder(folder)
            self._select(resolved, readonly=False)
            conn = self.conn()
            spec = ",".join(str(u) for u in uids)
            if read is not None:
                op = "+FLAGS" if read else "-FLAGS"
                status, data = conn.uid("STORE", spec, op, "(\\Seen)")
                if status != "OK":
                    raise ImapError(f"STORE \\Seen failed: {_first(data)}")
            if flagged is not None:
                op = "+FLAGS" if flagged else "-FLAGS"
                status, data = conn.uid("STORE", spec, op, "(\\Flagged)")
                if status != "OK":
                    raise ImapError(f"STORE \\Flagged failed: {_first(data)}")
            changed += len(uids)
        return {"updated": changed}

    def move(self, message_ids: List[str], dest_folder: str) -> Dict[str, Any]:
        dest = self._resolve_folder(dest_folder)
        moved = 0
        for folder, uids in _group_by_folder(message_ids).items():
            source = self._resolve_folder(folder)
            if source == dest:
                continue
            self._select(source, readonly=False)
            conn = self.conn()
            spec = ",".join(str(u) for u in uids)
            try:
                status, data = conn.uid("MOVE", spec, _mbox_str(dest))
            except imaplib.IMAP4.error as exc:
                # Servers without RFC 6851 answer BAD, which imaplib raises.
                status, data = "NO", [str(exc).encode()]
            if status != "OK":
                try:
                    status, data = conn.uid("COPY", spec, _mbox_str(dest))
                except imaplib.IMAP4.error as exc:
                    raise ImapError(f"COPY to '{dest}' failed: {exc}") from exc
                if status != "OK":
                    raise ImapError(f"COPY to '{dest}' failed: {_first(data)}")
                conn.uid("STORE", spec, "+FLAGS", "(\\Deleted)")
                self._expunge(uids)
            moved += len(uids)
        self._selected = None  # folder state changed underneath us
        return {"moved": moved, "destination": dest}

    def delete(self, message_ids: List[str], permanent: bool = False) -> Dict[str, Any]:
        if not permanent:
            trash = self.folder_for_role("trash")
            if not trash:
                # Never quietly escalate a reversible delete into an expunge.
                raise ImapError(
                    f"No Trash folder detected on '{self.account.name}', so a "
                    "recoverable delete is not possible. Use mail_move to file the "
                    "messages instead, or — with the user's explicit approval — call "
                    "again with permanent=true and confirm=true."
                )
            result = self.move(message_ids, trash)
            return {"deleted": result["moved"], "moved_to": trash, "permanent": False}
        removed = 0
        for folder, uids in _group_by_folder(message_ids).items():
            resolved = self._resolve_folder(folder)
            self._select(resolved, readonly=False)
            conn = self.conn()
            spec = ",".join(str(u) for u in uids)
            status, data = conn.uid("STORE", spec, "+FLAGS", "(\\Deleted)")
            if status != "OK":
                raise ImapError(f"Marking deleted failed: {_first(data)}")
            self._expunge(uids)
            removed += len(uids)
        self._selected = None
        return {"deleted": removed, "permanent": True}

    # --------------------------------------------------------- compose/send

    def build_message(
        self,
        to: List[str],
        subject: str,
        body: str,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        html_body: Optional[str] = None,
        attachments: Optional[List[str]] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[str] = None,
    ) -> EmailMessage:
        acct = self.account
        msg = EmailMessage()
        from_display = acct.display_name
        msg["From"] = f"{from_display} <{acct.email}>" if from_display else acct.email
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        if bcc:
            msg["Bcc"] = ", ".join(bcc)
        msg["Subject"] = subject
        msg["Date"] = format_datetime(datetime.now(timezone.utc))
        msg["Message-ID"] = make_msgid(domain=acct.email.split("@")[-1])
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = (references + " " + in_reply_to) if references else in_reply_to

        msg.set_content(body or "")
        if html_body:
            msg.add_alternative(html_body, subtype="html")

        for path in attachments or []:
            if not os.path.isfile(path):
                raise ImapError(f"Attachment not found: {path}")
            with open(path, "rb") as fh:
                data = fh.read()
            msg.add_attachment(
                data,
                maintype="application",
                subtype="octet-stream",
                filename=os.path.basename(path),
            )
        return msg

    def append_draft(self, msg: EmailMessage) -> Dict[str, Any]:
        drafts = self.folder_for_role("drafts")
        if not drafts:
            raise ImapError(
                f"No Drafts folder found on '{self.account.name}'. "
                "Check mail_list_folders and pass the folder name explicitly."
            )
        conn = self.conn()
        try:
            status, data = conn.append(
                _mbox(drafts),
                "(\\Draft)",
                imaplib.Time2Internaldate(time.time()),
                msg.as_bytes(),
            )
        except imaplib.IMAP4.error as exc:
            raise ImapError(f"Saving draft to '{drafts}' failed: {exc}") from exc
        if status != "OK":
            raise ImapError(f"Saving draft failed: {_first(data)}")
        self._folder_cache = None
        return {"saved_to_folder": drafts, "subject": msg["Subject"]}

    def send(self, msg: EmailMessage, save_to_sent: bool = True) -> Dict[str, Any]:
        acct = self.account
        context = ssl.create_default_context()
        try:
            if acct.smtp_mode == "ssl":
                server = smtplib.SMTP_SSL(
                    acct.smtp_host, acct.smtp_port, context=context, timeout=60
                )
            else:
                server = smtplib.SMTP(acct.smtp_host, acct.smtp_port, timeout=60)
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
        except (OSError, smtplib.SMTPException, ssl.SSLError) as exc:
            raise ImapError(
                f"Could not connect to SMTP {acct.smtp_host}:{acct.smtp_port}: {exc}"
            ) from exc
        try:
            server.login(acct.imap_user, acct.password)
            server.send_message(msg)
        except smtplib.SMTPAuthenticationError as exc:
            raise ImapError(
                f"SMTP authentication failed for '{acct.name}': {exc}. "
                f"{_password_hint(acct)}"
            ) from exc
        except smtplib.SMTPException as exc:
            raise ImapError(f"Sending failed: {exc}") from exc
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass

        result: Dict[str, Any] = {
            "sent": True,
            "to": msg["To"],
            "subject": msg["Subject"],
            "message_id": msg["Message-ID"],
        }
        if save_to_sent:
            sent = self.folder_for_role("sent")
            if sent:
                try:
                    self.conn().append(
                        _mbox(sent),
                        "(\\Seen)",
                        imaplib.Time2Internaldate(time.time()),
                        msg.as_bytes(),
                    )
                    result["copied_to"] = sent
                except Exception as exc:  # noqa: BLE001 - non-fatal
                    result["copy_to_sent_failed"] = str(exc)
        return result


# ------------------------------------------------------------------ parsing


def split_id(message_id: str) -> Tuple[str, int]:
    if "|" not in message_id:
        raise ImapError(
            f"Malformed message id '{message_id}'. IMAP ids look like 'INBOX|48213' "
            "and come from mail_search results."
        )
    folder, uid = message_id.rsplit("|", 1)
    if not uid.isdigit():
        raise ImapError(f"Malformed message id '{message_id}': '{uid}' is not a UID.")
    return folder, int(uid)


def _group_by_folder(message_ids: List[str]) -> Dict[str, List[int]]:
    grouped: Dict[str, List[int]] = {}
    for mid in message_ids:
        folder, uid = split_id(mid)
        grouped.setdefault(folder, []).append(uid)
    return grouped


def _chunks(items: List[int], size: int) -> Iterable[List[int]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _first(data: Any) -> str:
    if not data:
        return ""
    head = data[0]
    if isinstance(head, tuple):
        head = head[0]
    if isinstance(head, bytes):
        return head.decode("utf-8", "replace")
    return str(head)


def _int_field(text: str, key: str) -> Optional[int]:
    match = re.search(rf"{key}\s+(\d+)", text)
    return int(match.group(1)) if match else None


_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.*)$')


def _parse_list_line(raw: Any) -> Optional[Tuple[List[str], str]]:
    if isinstance(raw, tuple):
        raw = raw[0]
    if not isinstance(raw, bytes):
        return None
    match = _LIST_RE.match(raw.strip())
    if not match:
        return None
    flags = match.group("flags").decode("ascii", "replace").split()
    name_raw = match.group("name").strip()
    if name_raw.startswith(b'"') and name_raw.endswith(b'"'):
        name_raw = name_raw[1:-1]
    return flags, imap_utf7.decode(name_raw.replace(b'\\"', b'"'))


_FLAGS_RE = re.compile(rb"FLAGS\s+\(([^)]*)\)")
_UID_RE = re.compile(rb"UID\s+(\d+)")
_SIZE_RE = re.compile(rb"RFC822\.SIZE\s+(\d+)")
_INTERNALDATE_RE = re.compile(rb'INTERNALDATE\s+"([^"]+)"')


def _flags_from(data: Any) -> List[str]:
    for item in data:
        head = item[0] if isinstance(item, tuple) else item
        if isinstance(head, bytes):
            match = _FLAGS_RE.search(head)
            if match:
                return match.group(1).decode("ascii", "replace").split()
    return []


def _extract_literal(data: Any) -> Optional[bytes]:
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None


def _parse_fetch_summaries(
    data: Any, folder: str, account_name: str
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        meta, header_bytes = item[0], item[1]
        if not isinstance(meta, bytes) or not isinstance(header_bytes, bytes):
            continue
        uid_match = _UID_RE.search(meta)
        if not uid_match:
            continue
        uid = int(uid_match.group(1))
        flags = _FLAGS_RE.search(meta)
        flag_list = flags.group(1).decode("ascii", "replace").split() if flags else []
        size_match = _SIZE_RE.search(meta)
        idate_match = _INTERNALDATE_RE.search(meta)

        msg = email.message_from_bytes(header_bytes)
        summary = _message_from_email(msg, folder, uid, account_name, flag_list)
        summary["size"] = int(size_match.group(1)) if size_match else None
        if not summary.get("date") and idate_match:
            try:
                dt = imaplib.Internaldate2tuple(
                    b'INTERNALDATE "' + idate_match.group(1) + b'"'
                )
                if dt:
                    summary["date"] = datetime.fromtimestamp(
                        time.mktime(dt), timezone.utc
                    ).isoformat()
            except Exception:  # noqa: BLE001
                pass
        # BODYSTRUCTURE is in the metadata blob; a cheap, reliable-enough signal.
        upper = meta.upper()
        summary["has_attachments"] = b'"ATTACHMENT"' in upper or b"(ATTACHMENT" in upper
        out.append(summary)
    return out


def _message_from_email(
    msg: "email.message.Message",
    folder: str,
    uid: int,
    account_name: str,
    flags: List[str],
) -> Dict[str, Any]:
    return {
        "id": f"{folder}|{uid}",
        "account": account_name,
        "folder": folder,
        "subject": decode_header_value(msg.get("Subject")) or "(no subject)",
        "from": parse_address(msg.get("From")),
        "to": parse_address_list(msg.get("To")),
        "cc": parse_address_list(msg.get("Cc")),
        "date": parse_date(msg.get("Date")),
        "unread": "\\Seen" not in flags,
        "flagged": "\\Flagged" in flags,
        "answered": "\\Answered" in flags,
        "rfc_message_id": (msg.get("Message-ID") or "").strip() or None,
        "in_reply_to": (msg.get("In-Reply-To") or "").strip() or None,
        "references": (msg.get("References") or "").strip() or None,
        "list_id": decode_header_value(msg.get("List-Id")) or None,
    }


def _decode_part(part: "email.message.Message") -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    for cs in (charset, "utf-8", "gb18030", "big5", "latin-1"):
        try:
            return payload.decode(cs)
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", "replace")


def _iter_attachment_parts(msg: "email.message.Message"):
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if disposition == "attachment" or (filename and disposition != "inline"):
            yield part


def _extract_body(msg: "email.message.Message") -> Dict[str, Any]:
    text_parts: List[str] = []
    html_parts: List[str] = []
    attachments: List[Dict[str, Any]] = []

    index = 0
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if disposition == "attachment" or (filename and disposition != "inline"):
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                {
                    "id": str(index),
                    "filename": decode_header_value(filename or "") or f"part-{index}",
                    "content_type": part.get_content_type(),
                    "bytes": len(payload),
                }
            )
            index += 1
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            text_parts.append(_decode_part(part))
        elif ctype == "text/html":
            html_parts.append(_decode_part(part))

    if text_parts:
        body = "\n\n".join(p for p in text_parts if p.strip())
        body_format = "text"
    elif html_parts:
        body = html_to_text("\n".join(html_parts))
        body_format = "html->text"
    else:
        body = ""
        body_format = "none"

    body = strip_quoted_reply(body.strip())
    return {
        "body": body,
        "body_format": body_format,
        "snippet": make_snippet(body),
        "attachments": attachments,
        "has_attachments": bool(attachments),
    }


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    return cleaned or "attachment"


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    counter = 2
    while os.path.exists(f"{stem} ({counter}){ext}"):
        counter += 1
    return f"{stem} ({counter}){ext}"


def _password_hint(acct: Account) -> str:
    import i18n
    from config import PROVIDERS

    default = PROVIDERS.get(acct.provider, PROVIDERS["generic"]).get("password_hint", "")
    return i18n.provider_hint(acct.provider, default)
