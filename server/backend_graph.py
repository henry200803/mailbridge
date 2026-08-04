"""Microsoft Graph backend — personal Outlook.com/Hotmail/Live accounts and
work/school Exchange Online accounts (including university tenants).

Microsoft retired Basic authentication for IMAP/POP/SMTP on both consumer and
Exchange Online mailboxes, so Graph over OAuth is the only remaining option for
these accounts. Message ids are Graph's own opaque ids.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

import oauth_ms
from config import Account
from emailutil import (
    html_to_text,
    iso_to_datetime,
    make_snippet,
    strip_quoted_reply,
)

GRAPH = "https://graph.microsoft.com/v1.0"

WELL_KNOWN = {
    "inbox": "inbox",
    "drafts": "drafts",
    "sent": "sentitems",
    "sentitems": "sentitems",
    "trash": "deleteditems",
    "deleteditems": "deleteditems",
    "junk": "junkemail",
    "spam": "junkemail",
    "junkemail": "junkemail",
    "archive": "archive",
    "outbox": "outbox",
}

ROLE_BY_WELL_KNOWN = {
    "inbox": "inbox",
    "drafts": "drafts",
    "sentitems": "sent",
    "deleteditems": "trash",
    "junkemail": "junk",
    "archive": "archive",
}

SUMMARY_SELECT = (
    "id,conversationId,subject,from,toRecipients,ccRecipients,receivedDateTime,"
    "sentDateTime,isRead,flag,hasAttachments,bodyPreview,parentFolderId,importance,"
    "internetMessageId,webLink"
)


class GraphError(Exception):
    pass


def _qid(value: str) -> str:
    """Percent-encode a Graph id for use in a URL path segment.

    Graph message, folder and attachment ids are base64-flavoured and routinely
    contain '/', '+' and '='. urllib.parse.quote leaves '/' alone by default,
    which silently splits the path and produces a misleading 404.
    """
    return urllib.parse.quote(str(value), safe="")


class GraphBackend:
    def __init__(self, account: Account) -> None:
        self.account = account
        self._folders: Optional[List[Dict[str, Any]]] = None
        self._folder_names: Dict[str, str] = {}

    # -------------------------------------------------------------- request

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, str]] = None,
        raw_response: bool = False,
    ) -> Any:
        url = path if path.startswith("http") else GRAPH + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)

        for attempt in range(4):
            token = oauth_ms.get_access_token(self.account)
            data = json.dumps(body).encode("utf-8") if body is not None else None
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
            if data is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    payload = resp.read()
                    if raw_response:
                        return payload
                    if not payload:
                        return {}
                    return json.loads(payload.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")
                if exc.code in (429, 503, 504) and attempt < 3:
                    # Retry-After may be an HTTP-date rather than seconds.
                    try:
                        delay = int(exc.headers.get("Retry-After") or (2 ** attempt))
                    except (TypeError, ValueError):
                        delay = 2 ** attempt
                    time.sleep(min(delay, 30))
                    continue
                raise GraphError(self._explain(exc.code, text)) from exc
            except urllib.error.URLError as exc:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise GraphError(f"Network error reaching Microsoft Graph: {exc.reason}") from exc
        raise GraphError("Microsoft Graph kept throttling the request; try again shortly.")

    def _explain(self, code: int, text: str) -> str:
        try:
            detail = json.loads(text).get("error", {})
            msg = detail.get("message", text)
            err_code = detail.get("code", "")
        except (json.JSONDecodeError, AttributeError):
            msg, err_code = text[:600], ""

        if code == 401:
            return (
                f"Microsoft rejected the access token (401 {err_code}): {msg}\n"
                f"Re-authorise with: python3 <plugin>/server/setup.py auth {self.account.name}"
            )
        if code == 403:
            return (
                f"Access denied (403 {err_code}): {msg}\n"
                "This usually means the app registration is missing a delegated "
                "permission (Mail.ReadWrite / Mail.Send), or the tenant admin has not "
                "consented. Universities commonly block unapproved apps — you may need "
                "to ask IT to approve the app registration."
            )
        if code == 404:
            return (
                f"Not found (404): {msg}\nThe message or folder id may be stale — "
                "search again to get current ids."
            )
        return f"Microsoft Graph error {code} {err_code}: {msg}"

    def _paged(self, path: str, params: Dict[str, str], limit: int) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        page = self._request("GET", path, params=params)
        while True:
            out.extend(page.get("value", []))
            nxt = page.get("@odata.nextLink")
            if not nxt or len(out) >= limit:
                break
            page = self._request("GET", nxt)
        return out[:limit]

    # -------------------------------------------------------------- folders

    def list_folders(self, refresh: bool = False) -> List[Dict[str, Any]]:
        if self._folders is not None and not refresh:
            return self._folders
        # mailFolder has no wellKnownName property on the v1.0 endpoint (beta
        # only — v1.0 answers 400 to it), so roles are resolved by asking each
        # well-known name for its folder id directly.
        role_by_id: Dict[str, str] = {}
        for wkn in ROLE_BY_WELL_KNOWN:
            try:
                node = self._request(
                    "GET", f"/me/mailFolders/{wkn}", params={"$select": "id"}
                )
            except GraphError:
                continue  # e.g. a mailbox that has never archived answers 404
            if node.get("id"):
                role_by_id[node["id"]] = ROLE_BY_WELL_KNOWN[wkn]

        select = "id,displayName,unreadItemCount,totalItemCount,childFolderCount"
        folders: List[Dict[str, Any]] = []

        # /me/mailFolders returns only top-level folders, and v1.0 rejects
        # multi-level $expand, so nested layouts like Inbox/Receipts are walked
        # with one childFolders request per non-leaf folder.
        def walk(base_path: str, prefix: str, depth: int) -> None:
            if depth > 6 or len(folders) >= 250:
                return
            items = self._paged(base_path, {"$top": "100", "$select": select}, limit=100)
            for item in items:
                leaf = item.get("displayName") or item["id"]
                path = f"{prefix}/{leaf}" if prefix else leaf
                folders.append(
                    {
                        "id": item["id"],
                        "name": path,
                        "leaf": leaf,
                        "role": role_by_id.get(item["id"]),
                        "unread": item.get("unreadItemCount"),
                        "total": item.get("totalItemCount"),
                    }
                )
                if item.get("childFolderCount"):
                    walk(
                        f"/me/mailFolders/{_qid(item['id'])}/childFolders",
                        path,
                        depth + 1,
                    )

        walk("/me/mailFolders", "", 0)
        self._folders = folders
        # Index both the full path and the bare leaf name, so "Receipts" resolves
        # as readily as "Inbox/Receipts". First writer wins for leaf collisions.
        index: Dict[str, str] = {}
        for folder in folders:
            index.setdefault(folder["name"].lower(), folder["id"])
        for folder in folders:
            index.setdefault(folder["leaf"].lower(), folder["id"])
        self._folder_names = index
        return folders

    def _resolve_folder(self, folder: Optional[str]) -> str:
        """Return a folder id or well-known name usable in a Graph path."""
        if not folder:
            return "inbox"
        key = folder.strip().lower()
        if key in WELL_KNOWN:
            return WELL_KNOWN[key]
        if key in ("all", "*", "allmail"):
            return ""
        self.list_folders()
        if folder in [f["id"] for f in (self._folders or [])]:
            return folder
        if key in self._folder_names:
            return self._folder_names[key]
        names = [f["name"] for f in (self._folders or [])]
        raise GraphError(
            f"Folder '{folder}' not found on '{self.account.name}'. "
            f"Available: {', '.join(names[:40])}"
        )

    # --------------------------------------------------------------- search

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
        target = self._resolve_folder(folder if folder is not None else "inbox")
        base = f"/me/mailFolders/{_qid(target)}/messages" if target else "/me/messages"

        # Graph does not support contains() or any() lambdas in $filter on
        # messages, so every substring match has to go through $search — which
        # in turn cannot be combined with $filter or $orderby. Anything $search
        # can't express is filtered and sorted client-side afterwards.
        use_search = bool(query or sender or recipient or subject)
        since_dt = iso_to_datetime(since)
        before_dt = iso_to_datetime(before)

        if use_search:
            terms: List[str] = []
            if query:
                terms.append(_kql_value(query.strip()))
            if sender:
                terms.append(f"from:{_kql_value(sender)}")
            if recipient:
                terms.append(f"to:{_kql_value(recipient)}")
            if subject:
                terms.append(f"subject:{_kql_value(subject)}")
            if has_attachment:
                terms.append("hasAttachments:true")
            if since_dt:
                terms.append(f'received>={since_dt.strftime("%Y-%m-%d")}')
            if before_dt:
                terms.append(f'received<{before_dt.strftime("%Y-%m-%d")}')

            # The whole KQL expression is one double-quoted $search value, so
            # inner quotes must be escaped rather than nested raw.
            kql = " AND ".join(terms)
            params = {
                "$search": '"' + kql.replace('"', '\\"') + '"',
                "$select": SUMMARY_SELECT,
                "$top": str(min(max(limit * 3, 25), 100)),
            }
            items = self._paged(base, params, limit=max(limit * 3, 50))
            if unread_only:
                items = [m for m in items if not m.get("isRead")]
            if flagged_only:
                items = [
                    m for m in items
                    if (m.get("flag") or {}).get("flagStatus") == "flagged"
                ]
            items.sort(key=lambda m: m.get("receivedDateTime") or "", reverse=True)
            items = items[:limit]
        else:
            filters: List[str] = []
            if unread_only:
                filters.append("isRead eq false")
            if flagged_only:
                filters.append("flag/flagStatus eq 'flagged'")
            if has_attachment:
                filters.append("hasAttachments eq true")
            if since_dt:
                filters.append(f"receivedDateTime ge {since_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}")
            if before_dt:
                filters.append(f"receivedDateTime lt {before_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}")

            params = {
                "$select": SUMMARY_SELECT,
                "$orderby": "receivedDateTime desc",
                "$top": str(min(max(limit, 10), 100)),
            }
            if filters:
                params["$filter"] = " and ".join(filters)
            items = self._paged(base, params, limit=limit)

        return [self._summary(m) for m in items]

    # ------------------------------------------------------------ read one

    def get_message(
        self, message_id: str, include_body: bool = True, max_body_chars: int = 8000
    ) -> Dict[str, Any]:
        select = SUMMARY_SELECT + (",body,bccRecipients,replyTo" if include_body else "")
        msg = self._request(
            "GET", f"/me/messages/{_qid(message_id)}", params={"$select": select}
        )
        out = self._summary(msg)
        out["bcc"] = _addresses(msg.get("bccRecipients"))
        if include_body:
            body = msg.get("body") or {}
            content = body.get("content") or ""
            if (body.get("contentType") or "").lower() == "html":
                text = html_to_text(content)
                out["body_format"] = "html->text"
            else:
                text = content
                out["body_format"] = "text"
            text = strip_quoted_reply(text.strip())
            out["body"] = text
            out["snippet"] = make_snippet(text)
            if msg.get("hasAttachments"):
                out["attachments"] = self.list_attachments(message_id)
            else:
                out["attachments"] = []
        return out

    def get_thread(self, message_id: str, limit: int = 30) -> List[Dict[str, Any]]:
        msg = self._request(
            "GET",
            f"/me/messages/{_qid(message_id)}",
            params={"$select": "conversationId"},
        )
        conv = msg.get("conversationId")
        if not conv:
            return [self.get_message(message_id)]
        # A conversationId $filter cannot be combined with $orderby — Graph
        # answers 400 InefficientFilter. Fetch unordered, sort client-side.
        items = self._paged(
            "/me/messages",
            {
                "$filter": f"conversationId eq '{_escape_odata(conv)}'",
                "$select": SUMMARY_SELECT,
                "$top": str(min(limit, 100)),
            },
            limit=limit,
        )
        items.sort(key=lambda m: m.get("receivedDateTime") or m.get("sentDateTime") or "")
        return [self._summary(m) for m in items]

    def list_attachments(self, message_id: str) -> List[Dict[str, Any]]:
        items = self._request(
            "GET",
            f"/me/messages/{_qid(message_id)}/attachments",
            params={"$select": "id,name,contentType,size,isInline"},
        ).get("value", [])
        return [
            {
                "id": a["id"],
                "filename": a.get("name") or "attachment",
                "content_type": a.get("contentType"),
                "bytes": a.get("size"),
                "inline": a.get("isInline", False),
            }
            for a in items
            if not a.get("isInline")
        ]

    def save_attachment(
        self, message_id: str, attachment_id: str, dest_dir: str
    ) -> Dict[str, Any]:
        listing = self._request(
            "GET",
            f"/me/messages/{_qid(message_id)}/attachments",
            params={"$select": "id,name,contentType,size"},
        ).get("value", [])
        target = None
        for att in listing:
            if attachment_id in (att.get("id"), att.get("name")):
                target = att
                break
        if target is None:
            names = ", ".join(a.get("name", "?") for a in listing) or "(none)"
            raise GraphError(
                f"No attachment '{attachment_id}' on that message. Available: {names}"
            )
        full = self._request(
            "GET",
            f"/me/messages/{_qid(message_id)}/attachments/"
            f"{_qid(target['id'])}",
        )
        content = full.get("contentBytes")
        if content is None:
            raise GraphError(
                f"Attachment '{target.get('name')}' has no downloadable content "
                "(it may be a linked cloud file rather than a real attachment)."
            )
        payload = base64.b64decode(content)
        os.makedirs(dest_dir, exist_ok=True)
        safe = _safe_filename(target.get("name") or "attachment")
        path = _unique_path(os.path.join(dest_dir, safe))
        with open(path, "wb") as fh:
            fh.write(payload)
        return {
            "saved_to": path,
            "filename": safe,
            "bytes": len(payload),
            "content_type": target.get("contentType"),
        }

    # ------------------------------------------------------------ mutations

    def set_flags(
        self,
        message_ids: List[str],
        read: Optional[bool] = None,
        flagged: Optional[bool] = None,
    ) -> Dict[str, Any]:
        patch: Dict[str, Any] = {}
        if read is not None:
            patch["isRead"] = bool(read)
        if flagged is not None:
            patch["flag"] = {"flagStatus": "flagged" if flagged else "notFlagged"}
        if not patch:
            return {"updated": 0}
        for mid in message_ids:
            self._request("PATCH", f"/me/messages/{_qid(mid)}", body=patch)
        return {"updated": len(message_ids)}

    def move(self, message_ids: List[str], dest_folder: str) -> Dict[str, Any]:
        dest = self._resolve_folder(dest_folder)
        if not dest:
            raise GraphError("A destination folder is required.")
        for mid in message_ids:
            self._request(
                "POST",
                f"/me/messages/{_qid(mid)}/move",
                body={"destinationId": dest},
            )
        return {"moved": len(message_ids), "destination": dest_folder}

    def delete(self, message_ids: List[str], permanent: bool = False) -> Dict[str, Any]:
        if not permanent:
            self.move(message_ids, "deleteditems")
            return {
                "deleted": len(message_ids),
                "moved_to": "Deleted Items",
                "permanent": False,
            }
        for mid in message_ids:
            self._request("DELETE", f"/me/messages/{_qid(mid)}")
        return {"deleted": len(message_ids), "permanent": True}

    # -------------------------------------------------------- compose/send

    def _payload(
        self,
        to: List[str],
        subject: str,
        body: str,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        html_body: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "subject": subject,
            "body": {
                "contentType": "HTML" if html_body else "Text",
                "content": html_body or body,
            },
            "toRecipients": _recipients(to),
            "ccRecipients": _recipients(cc),
            "bccRecipients": _recipients(bcc),
        }

    def create_draft(
        self,
        to: List[str],
        subject: str,
        body: str,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        html_body: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
        attachments: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        if reply_to_message_id:
            draft = self._request(
                "POST",
                f"/me/messages/{_qid(reply_to_message_id)}/createReply",
                body={},
            )
            patch: Dict[str, Any] = {
                "body": {
                    "contentType": "HTML" if html_body else "Text",
                    "content": html_body or body,
                }
            }
            if to:
                patch["toRecipients"] = _recipients(to)
            if cc:
                patch["ccRecipients"] = _recipients(cc)
            if subject:
                patch["subject"] = subject
            draft = self._request(
                "PATCH", f"/me/messages/{_qid(draft['id'])}", body=patch
            )
        else:
            draft = self._request(
                "POST", "/me/messages", body=self._payload(to, subject, body, cc, bcc, html_body)
            )
        for path in attachments or []:
            self._attach(draft["id"], path)
        return {
            "draft_id": draft["id"],
            "subject": draft.get("subject"),
            "saved_to_folder": "Drafts",
            "web_link": draft.get("webLink"),
        }

    def _attach(self, message_id: str, path: str) -> None:
        if not os.path.isfile(path):
            raise GraphError(f"Attachment not found: {path}")
        size = os.path.getsize(path)
        if size > 3 * 1024 * 1024:
            raise GraphError(
                f"Attachment '{os.path.basename(path)}' is {size // 1024 // 1024} MB. "
                "Files over 3 MB need Graph's upload-session API, which this server "
                "does not implement yet — send it another way."
            )
        with open(path, "rb") as fh:
            data = fh.read()
        self._request(
            "POST",
            f"/me/messages/{_qid(message_id)}/attachments",
            body={
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": os.path.basename(path),
                "contentBytes": base64.b64encode(data).decode("ascii"),
            },
        )

    def send(
        self,
        to: List[str],
        subject: str,
        body: str,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        html_body: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
        attachments: Optional[List[str]] = None,
        save_to_sent: bool = True,
    ) -> Dict[str, Any]:
        # Always build a draft first so attachments and reply threading work,
        # then send it. This also gives a stable id if sending fails.
        draft = self.create_draft(
            to, subject, body, cc, bcc, html_body, reply_to_message_id, attachments
        )
        self._request("POST", f"/me/messages/{_qid(draft['draft_id'])}/send", body={})
        # The draft-then-send path always files a copy in Sent Items; Graph has
        # no way to opt out of that once the message exists as a draft.
        return {"sent": True, "to": to, "subject": subject, "saved_to_sent": True}

    # -------------------------------------------------------------- helpers

    def _summary(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        flag = (msg.get("flag") or {}).get("flagStatus")
        return {
            "id": msg.get("id"),
            "account": self.account.name,
            "folder": self._folder_name(msg.get("parentFolderId")),
            "thread_id": msg.get("conversationId"),
            "subject": msg.get("subject") or "(no subject)",
            "from": _address(msg.get("from")),
            "to": _addresses(msg.get("toRecipients")),
            "cc": _addresses(msg.get("ccRecipients")),
            "date": msg.get("receivedDateTime") or msg.get("sentDateTime"),
            "unread": not msg.get("isRead", True),
            "flagged": flag == "flagged",
            "has_attachments": bool(msg.get("hasAttachments")),
            "importance": msg.get("importance"),
            "snippet": make_snippet(msg.get("bodyPreview") or ""),
            "rfc_message_id": msg.get("internetMessageId"),
            "web_link": msg.get("webLink"),
        }

    def _folder_name(self, folder_id: Optional[str]) -> Optional[str]:
        if not folder_id:
            return None
        for folder in self._folders or []:
            if folder["id"] == folder_id:
                return folder["name"]
        return None


# ------------------------------------------------------------------ helpers


def _recipients(addresses: Optional[List[str]]) -> List[Dict[str, Any]]:
    return [{"emailAddress": {"address": a}} for a in (addresses or []) if a]


def _address(node: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    if not node:
        return None
    inner = node.get("emailAddress") or {}
    return {"name": inner.get("name") or "", "email": inner.get("address") or ""}


def _addresses(nodes: Optional[List[Dict[str, Any]]]) -> List[Dict[str, str]]:
    return [a for a in (_address(n) for n in (nodes or [])) if a]


def _escape_odata(value: str) -> str:
    return value.replace("'", "''")


def _kql_value(value: str) -> str:
    """Render a KQL term. Multi-word values need quoting; single tokens don't."""
    cleaned = value.replace('"', " ").strip()
    if not cleaned:
        return '""'
    return f'"{cleaned}"' if re.search(r"\s", cleaned) else cleaned


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
