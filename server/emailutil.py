"""Shared helpers for decoding and normalising email data."""

from __future__ import annotations

import email.header
import email.utils
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_WS = re.compile(r"[ \t\r\n]+")


def decode_header_value(raw: Optional[str]) -> str:
    """Decode an RFC 2047 encoded header into a plain string."""
    if not raw:
        return ""
    try:
        parts = email.header.decode_header(raw)
    except Exception:  # noqa: BLE001 - malformed headers are common in the wild
        return str(raw)
    out: List[str] = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            for cs in (charset, "utf-8", "gb18030", "latin-1"):
                if not cs:
                    continue
                try:
                    out.append(chunk.decode(cs))
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                out.append(chunk.decode("utf-8", "replace"))
        else:
            out.append(chunk)
    return _WS.sub(" ", "".join(out)).strip()


def parse_address(raw: Optional[str]) -> Optional[Dict[str, str]]:
    if not raw:
        return None
    name, addr = email.utils.parseaddr(raw)
    return {"name": decode_header_value(name), "email": addr}


def parse_address_list(raw: Optional[str]) -> List[Dict[str, str]]:
    if not raw:
        return []
    out: List[Dict[str, str]] = []
    for name, addr in email.utils.getaddresses([raw]):
        if not addr and not name:
            continue
        out.append({"name": decode_header_value(name), "email": addr})
    return out


def parse_date(raw: Optional[str]) -> Optional[str]:
    """Return an ISO-8601 timestamp, or None if the header is unparseable."""
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def iso_to_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%b-%Y"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def html_to_text(html: str) -> str:
    """Crude but dependency-free HTML to text. Good enough for reading email."""
    text = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "  - ", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = _unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


_ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&#39;": "'", "&apos;": "'", "&mdash;": "—", "&ndash;": "–", "&hellip;": "…",
    "&rsquo;": "’", "&lsquo;": "‘", "&ldquo;": "“", "&rdquo;": "”", "&copy;": "©",
    "&reg;": "®", "&trade;": "™", "&zwnj;": "", "&#8203;": "",
}


def _unescape(text: str) -> str:
    for k, v in _ENTITIES.items():
        text = text.replace(k, v)

    def _num(match: "re.Match[str]") -> str:
        body = match.group(1)
        try:
            code = int(body[1:], 16) if body[:1].lower() == "x" else int(body)
            return chr(code)
        except (ValueError, OverflowError):
            return match.group(0)

    return re.sub(r"&#(x?[0-9a-fA-F]+);", _num, text)


def make_snippet(body: str, limit: int = 220) -> str:
    if not body:
        return ""
    flat = _WS.sub(" ", body).strip()
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def strip_quoted_reply(text: str) -> str:
    """Trim the quoted tail of a reply so summaries stay readable."""
    if not text:
        return text
    lines = text.splitlines()
    markers = (
        "-----Original Message-----",
        "________________________________",
        "-------- Forwarded Message --------",
    )
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if any(stripped.startswith(m) for m in markers):
            return "\n".join(lines[:idx]).rstrip()
        if re.match(r"^On .{10,80} wrote:$", stripped):
            return "\n".join(lines[:idx]).rstrip()
        if re.match(r"^在.{4,60}(写道|寫道)[：:]$", stripped):
            return "\n".join(lines[:idx]).rstrip()
    return text


def truncate(text: str, limit: int) -> Dict[str, Any]:
    """Return {'text', 'truncated', 'total_chars'} for context-safe bodies."""
    if limit <= 0 or len(text) <= limit:
        return {"text": text, "truncated": False, "total_chars": len(text)}
    return {
        "text": text[:limit] + f"\n\n[... truncated, {len(text) - limit} more characters. "
        "Call mail_get_message again with a larger max_body_chars to read the rest.]",
        "truncated": True,
        "total_chars": len(text),
    }


def format_size(num: Optional[int]) -> str:
    if not num:
        return "0 B"
    step = 1024.0
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < step:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} TB"
