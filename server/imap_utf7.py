"""IMAP modified UTF-7 (RFC 3501 section 5.1.3) encode/decode.

Needed for mailbox names with non-ASCII characters, which is the normal case on
Chinese providers (163, QQ) where folders are named 已发送 / 已删除 etc.
"""

from __future__ import annotations

import binascii


def encode(name: str) -> bytes:
    """Encode a unicode mailbox name to IMAP modified UTF-7 bytes."""
    out = bytearray()
    buf: list = []

    def flush() -> None:
        if not buf:
            return
        raw = "".join(buf).encode("utf-16-be")
        b64 = binascii.b2a_base64(raw).decode("ascii").rstrip("\n").rstrip("=")
        out.extend(b"&" + b64.replace("/", ",").encode("ascii") + b"-")
        buf.clear()

    for ch in name:
        ordinal = ord(ch)
        if ch == "&":
            flush()
            out.extend(b"&-")
        elif 0x20 <= ordinal <= 0x7E:
            flush()
            out.append(ordinal)
        else:
            buf.append(ch)
    flush()
    return bytes(out)


def decode(data) -> str:
    """Decode IMAP modified UTF-7 bytes (or str) to a unicode mailbox name."""
    if isinstance(data, bytes):
        text = data.decode("ascii", "replace")
    else:
        text = data

    out: list = []
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch != "&":
            out.append(ch)
            i += 1
            continue
        end = text.find("-", i + 1)
        if end == -1:
            # Malformed; emit the rest verbatim rather than blowing up.
            out.append(text[i:])
            break
        chunk = text[i + 1 : end]
        if chunk == "":
            out.append("&")
        else:
            b64 = chunk.replace(",", "/")
            b64 += "=" * ((4 - len(b64) % 4) % 4)
            try:
                out.append(binascii.a2b_base64(b64.encode("ascii")).decode("utf-16-be"))
            except (binascii.Error, UnicodeDecodeError, ValueError):
                out.append(text[i : end + 1])
        i = end + 1
    return "".join(out)
