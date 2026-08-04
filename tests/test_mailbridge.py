#!/usr/bin/env python3
"""Offline tests for mailbridge: MCP protocol handshake, IMAP response parsing,
folder-name encoding, and body extraction. No network, no real credentials.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.join(os.path.dirname(HERE), "server")
sys.path.insert(0, SERVER_DIR)

import imap_utf7  # noqa: E402
from emailutil import (  # noqa: E402
    decode_header_value,
    html_to_text,
    make_snippet,
    parse_address_list,
    parse_date,
    strip_quoted_reply,
    truncate,
)


# ---------------------------------------------------------------- MCP protocol


class TestMcpProtocol(unittest.TestCase):
    """Drive server.py over stdio exactly the way a real MCP client would."""

    def _talk(self, messages, env=None):
        full_env = dict(os.environ)
        full_env.setdefault("PYTHONIOENCODING", "utf-8")
        if env:
            full_env.update(env)
        payload = "\n".join(json.dumps(m) for m in messages) + "\n"
        proc = subprocess.run(
            [sys.executable, os.path.join(SERVER_DIR, "server.py")],
            input=payload,
            capture_output=True,
            encoding="utf-8",
            timeout=60,
            env=full_env,
        )
        out = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out, proc.stderr

    def test_initialize_handshake(self):
        replies, _ = self._talk(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
            ]
        )
        self.assertEqual(len(replies), 1)
        result = replies[0]["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "mailbridge")
        self.assertIn("tools", result["capabilities"])
        self.assertIn("mail_search", result["instructions"])

    def test_protocol_version_fallback(self):
        replies, _ = self._talk(
            [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "1999-01-01", "capabilities": {}}}]
        )
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")

    def test_older_protocol_is_echoed(self):
        replies, _ = self._talk(
            [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {}}}]
        )
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2024-11-05")

    def test_tools_list_is_well_formed(self):
        replies, _ = self._talk(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
        )
        tools = replies[-1]["result"]["tools"]
        names = {t["name"] for t in tools}
        expected = {
            "mail_list_accounts", "mail_list_folders", "mail_search",
            "mail_get_message", "mail_get_thread", "mail_download_attachment",
            "mail_digest", "mail_export", "mail_draft", "mail_send",
            "mail_mark", "mail_move", "mail_delete", "mail_auth_status",
        }
        self.assertEqual(names, expected)
        for tool in tools:
            self.assertTrue(tool["description"].strip(), tool["name"])
            schema = tool["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertIn("properties", schema)
            for prop, spec in schema["properties"].items():
                self.assertIn("type", spec, f"{tool['name']}.{prop}")

    def test_unknown_tool_is_a_protocol_error(self):
        replies, _ = self._talk(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "mail_nope", "arguments": {}}},
            ]
        )
        self.assertIn("error", replies[-1])
        self.assertIn("mail_search", replies[-1]["error"]["message"])

    def test_no_config_gives_actionable_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            replies, _ = self._talk(
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "mail_list_accounts", "arguments": {}}},
                ],
                env={"MAILBRIDGE_HOME": tmp,
                     "MAILBRIDGE_CONFIG": os.path.join(tmp, "accounts.json")},
            )
            body = replies[-1]["result"]["content"][0]["text"]
            self.assertIn("setup.py", body)
            self.assertFalse(replies[-1]["result"]["isError"])

    def test_send_refuses_without_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "accounts.json")
            with open(cfg, "w") as fh:
                json.dump(
                    {"accounts": [{"name": "test", "kind": "imap", "provider": "icloud",
                                   "email": "a@icloud.com", "password": "x"}],
                     "default_account": "test"}, fh)
            replies, _ = self._talk(
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "mail_send", "arguments": {
                         "to": ["b@example.com"], "subject": "hi",
                         "body": "hello", "confirm": False}}},
                ],
                env={"MAILBRIDGE_HOME": tmp, "MAILBRIDGE_CONFIG": cfg},
            )
            result = replies[-1]["result"]
            self.assertTrue(result["isError"])
            self.assertIn("confirm=true", result["content"][0]["text"])

    def test_permanent_delete_refuses_without_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "accounts.json")
            with open(cfg, "w") as fh:
                json.dump(
                    {"accounts": [{"name": "test", "kind": "imap", "provider": "icloud",
                                   "email": "a@icloud.com", "password": "x"}]}, fh)
            replies, _ = self._talk(
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "mail_delete", "arguments": {
                         "message_ids": ["INBOX|1"], "permanent": True}}},
                ],
                env={"MAILBRIDGE_HOME": tmp, "MAILBRIDGE_CONFIG": cfg},
            )
            result = replies[-1]["result"]
            self.assertTrue(result["isError"])
            self.assertIn("confirm=true", result["content"][0]["text"])

    def test_language_switch_changes_tool_descriptions(self):
        with tempfile.TemporaryDirectory() as tmp:
            replies, _ = self._talk(
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                ],
                env={"MAILBRIDGE_LANG": "zh", "MAILBRIDGE_HOME": tmp,
                     "MAILBRIDGE_CONFIG": os.path.join(tmp, "accounts.json")},
            )
            self.assertIn("mailbridge 提供", replies[0]["result"]["instructions"])
            tools = {t["name"]: t for t in replies[-1]["result"]["tools"]}
            self.assertIn("列出全部已配置邮箱", tools["mail_list_accounts"]["description"])
            sender = tools["mail_search"]["inputSchema"]["properties"]["sender"]
            self.assertIn("发件人", sender["description"])

    def test_default_language_is_english(self):
        with tempfile.TemporaryDirectory() as tmp:
            replies, _ = self._talk(
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                ],
                env={"MAILBRIDGE_LANG": "", "MAILBRIDGE_HOME": tmp,
                     "MAILBRIDGE_CONFIG": os.path.join(tmp, "accounts.json")},
            )
            tools = {t["name"]: t for t in replies[-1]["result"]["tools"]}
            self.assertIn(
                "List every configured mailbox",
                tools["mail_list_accounts"]["description"],
            )

    def test_bad_json_does_not_kill_the_server(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(SERVER_DIR, "server.py")],
            input='{not json}\n{"jsonrpc":"2.0","id":1,"method":"initialize",'
                  '"params":{"protocolVersion":"2025-06-18","capabilities":{}}}\n',
            capture_output=True, encoding="utf-8", timeout=60,
        )
        lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
        self.assertEqual(lines[0]["error"]["code"], -32700)
        self.assertEqual(lines[1]["result"]["serverInfo"]["name"], "mailbridge")


# ------------------------------------------------------------------ utilities


class TestImapUtf7(unittest.TestCase):
    def test_round_trip(self):
        for name in [
            "INBOX", "Sent Items", "已发送", "已删除", "工作/项目 A",
            "R&D", "Travel & Receipts", "受信箱", "Ordner Ünïcode",
        ]:
            with self.subTest(name=name):
                self.assertEqual(imap_utf7.decode(imap_utf7.encode(name)), name)

    def test_known_encodings(self):
        # Values from RFC 3501 / real server traffic.
        self.assertEqual(imap_utf7.encode("~peter/mail/台北/日本語"),
                         b"~peter/mail/&U,BTFw-/&ZeVnLIqe-")
        self.assertEqual(imap_utf7.decode(b"&-"), "&")
        self.assertEqual(imap_utf7.decode(b"INBOX"), "INBOX")

    def test_malformed_input_does_not_raise(self):
        self.assertIsInstance(imap_utf7.decode(b"&broken"), str)
        self.assertIsInstance(imap_utf7.decode(b"&!!!!-x"), str)


class TestEmailUtil(unittest.TestCase):
    def test_decode_rfc2047_headers(self):
        self.assertEqual(
            decode_header_value("=?utf-8?B?5rWL6K+V6YKu5Lu2?="), "测试邮件"
        )
        self.assertEqual(
            decode_header_value("=?ISO-8859-1?Q?Caf=E9_meeting?="), "Café meeting"
        )
        self.assertEqual(decode_header_value("Plain subject"), "Plain subject")
        self.assertEqual(decode_header_value(None), "")

    def test_address_parsing(self):
        parsed = parse_address_list(
            '"Doe, Jane" <jane@example.com>, bob@example.org'
        )
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["email"], "jane@example.com")
        self.assertEqual(parsed[0]["name"], "Doe, Jane")
        self.assertEqual(parsed[1]["email"], "bob@example.org")

    def test_date_parsing(self):
        iso = parse_date("Sat, 1 Aug 2026 09:30:00 +1000")
        self.assertIsNotNone(iso)
        self.assertTrue(iso.startswith("2026-08-01T09:30:00"))
        self.assertIsNone(parse_date("not a date"))
        self.assertIsNone(parse_date(None))

    def test_html_to_text(self):
        html = (
            "<html><head><style>p{color:red}</style></head><body>"
            "<p>Hello&nbsp;<b>world</b></p><br><ul><li>one</li><li>two</li></ul>"
            "<script>alert(1)</script></body></html>"
        )
        text = html_to_text(html)
        self.assertIn("Hello world", text)
        self.assertIn("- one", text)
        self.assertNotIn("alert", text)
        self.assertNotIn("color:red", text)

    def test_html_entities(self):
        self.assertEqual(html_to_text("<p>A &amp; B &#8212; C &#x2014; D</p>"),
                         "A & B — C — D")

    def test_strip_quoted_reply(self):
        text = "My answer.\n\nOn Mon, 1 Jan 2026 at 10:00, Bob wrote:\n> old stuff"
        self.assertEqual(strip_quoted_reply(text), "My answer.")
        chinese = "好的。\n\n在 2026年1月1日，Bob 写道：\n> 旧内容"
        self.assertEqual(strip_quoted_reply(chinese), "好的。")

    def test_truncate_reports_totals(self):
        result = truncate("x" * 500, 100)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["total_chars"], 500)
        self.assertIn("max_body_chars", result["text"])
        short = truncate("hello", 100)
        self.assertFalse(short["truncated"])
        self.assertEqual(short["text"], "hello")

    def test_snippet(self):
        self.assertEqual(make_snippet("a\n\n  b   c "), "a b c")
        self.assertTrue(make_snippet("x" * 400).endswith("…"))


# ------------------------------------------------- IMAP parsing against fakes


class FakeImap:
    """Stands in for imaplib.IMAP4_SSL with canned, realistic responses."""

    def __init__(self, *args, **kwargs):
        self.selected = None
        self.commands = []
        self.plain_expunges = 0
        self.fail_move = False
        self.capabilities = ("IMAP4REV1", "UIDPLUS", "MOVE")

    def login(self, user, password):
        self.commands.append(("login", user))
        return ("OK", [b"LOGIN completed"])

    def _simple_command(self, name, *args):
        self.commands.append((name,) + args)
        import imaplib as _il
        if name not in _il.Commands:
            raise KeyError(name)
        return ("OK", [b"done"])

    def _untagged_response(self, typ, dat, name):
        return (typ, dat)

    def noop(self):
        return ("OK", [b"NOOP"])

    def logout(self):
        return ("BYE", [b"bye"])

    def list(self):
        return ("OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Sent) "/" "Sent Messages"',
            b'(\\HasNoChildren \\Drafts) "/" "Drafts"',
            b'(\\HasNoChildren \\Trash) "/" "Deleted Messages"',
            b'(\\HasNoChildren \\Junk) "/" "Junk"',
            b'(\\HasNoChildren) "/" "&XfJT0ZAB-"',  # 已发送 in modified UTF-7
            b'(\\Noselect \\HasChildren) "/" "Archive"',
        ])

    def select(self, mailbox, readonly=True):
        self.selected = mailbox
        return ("OK", [b"3"])

    def status(self, mailbox, what):
        return ("OK", [b'"INBOX" (MESSAGES 128 UNSEEN 7 RECENT 2)'])

    def uid(self, command, *args):
        self.commands.append((command,) + args)
        if command == "SEARCH":
            return ("OK", [b"101 102 103"])
        if command == "FETCH":
            return ("OK", self._fetch_response())
        if command == "MOVE" and self.fail_move:
            import imaplib as _il
            raise _il.IMAP4.error("BAD command unknown: MOVE")
        if command == "EXPUNGE":
            if "UIDPLUS" not in self.capabilities:
                return ("NO", [b"UIDPLUS not supported"])
            return ("OK", [b"expunged"])
        if command in ("STORE", "MOVE", "COPY"):
            return ("OK", [b"done"])
        return ("NO", [b"unsupported"])

    def expunge(self):
        self.plain_expunges += 1
        return ("OK", [b"expunged"])

    @staticmethod
    def _fetch_response():
        headers = (
            b"From: =?utf-8?B?5byg5LiJ?= <zhang@example.com>\r\n"
            b"To: me@icloud.com\r\n"
            b"Cc: team@example.com\r\n"
            b"Subject: =?utf-8?B?6YCa55+lOiDpobnnm67mm7TmlrA=?=\r\n"
            b"Date: Sat, 1 Aug 2026 09:30:00 +1000\r\n"
            b"Message-ID: <abc123@example.com>\r\n"
            b"\r\n"
        )
        meta = (
            b'103 (UID 103 FLAGS (\\Seen \\Flagged) RFC822.SIZE 4211 '
            b'INTERNALDATE "01-Aug-2026 09:30:00 +1000" '
            b'BODYSTRUCTURE (("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 10 1)'
            b'("APPLICATION" "PDF" ("NAME" "report.pdf") NIL NIL "BASE64" 8000 '
            b'NIL ("ATTACHMENT" ("FILENAME" "report.pdf"))) "MIXED") '
            b'BODY[HEADER.FIELDS (FROM TO CC BCC SUBJECT DATE MESSAGE-ID '
            b'IN-REPLY-TO REFERENCES LIST-ID)] {%d}' % len(headers)
        )
        return [(meta, headers), b")"]


class TestImapBackend(unittest.TestCase):
    def setUp(self):
        import backend_imap
        from config import Account

        self.backend_imap = backend_imap
        self._real = backend_imap.imaplib.IMAP4_SSL
        backend_imap.imaplib.IMAP4_SSL = FakeImap
        self.account = Account({
            "name": "icloud", "kind": "imap", "provider": "icloud",
            "email": "me@icloud.com", "password": "app-specific",
        })
        self.backend = backend_imap.ImapBackend(self.account)

    def tearDown(self):
        self.backend_imap.imaplib.IMAP4_SSL = self._real

    def test_folder_roles_and_utf7_names(self):
        folders = self.backend.list_folders()
        names = {f["name"] for f in folders}
        self.assertIn("INBOX", names)
        self.assertIn("已发送", names)  # decoded from modified UTF-7
        self.assertNotIn("Archive", names)  # \Noselect is skipped
        # SPECIAL-USE flags must win over name matching: the fixture also has a
        # folder literally named 已发送, which ROLE_BY_NAME maps to "sent".
        self.assertEqual(self.backend.folder_for_role("inbox"), "INBOX")
        self.assertEqual(self.backend.folder_for_role("sent"), "Sent Messages")
        self.assertEqual(self.backend.folder_for_role("drafts"), "Drafts")
        self.assertEqual(self.backend.folder_for_role("trash"), "Deleted Messages")
        self.assertEqual(self.backend.folder_for_role("junk"), "Junk")
        self.assertIsNone(self.backend.folder_for_role("archive"))

    def test_role_aliases_resolve(self):
        self.assertEqual(self.backend._resolve_folder("trash"), "Deleted Messages")
        self.assertEqual(self.backend._resolve_folder("sent"), "Sent Messages")
        self.assertEqual(self.backend._resolve_folder("spam"), "Junk")
        self.assertEqual(self.backend._resolve_folder(None), "INBOX")
        self.assertEqual(self.backend._resolve_folder("inbox"), "INBOX")

    def test_unknown_folder_lists_alternatives(self):
        with self.assertRaises(self.backend_imap.ImapError) as ctx:
            self.backend._resolve_folder("Nope")
        self.assertIn("Available:", str(ctx.exception))

    def test_search_parses_summaries(self):
        results = self.backend.search(folder="inbox", query="项目", limit=10)
        self.assertEqual(len(results), 1)
        msg = results[0]
        self.assertEqual(msg["id"], "INBOX|103")
        self.assertEqual(msg["subject"], "通知: 项目更新")
        self.assertEqual(msg["from"], {"name": "张三", "email": "zhang@example.com"})
        self.assertEqual(msg["cc"][0]["email"], "team@example.com")
        self.assertFalse(msg["unread"])          # \Seen present
        self.assertTrue(msg["flagged"])          # \Flagged present
        self.assertTrue(msg["has_attachments"])  # from BODYSTRUCTURE
        self.assertEqual(msg["size"], 4211)
        self.assertTrue(msg["date"].startswith("2026-08-01T09:30:00"))

    def test_search_builds_expected_criteria(self):
        self.backend.search(
            folder="inbox", sender="bob@x.com", subject="invoice",
            since="2026-01-01", unread_only=True, limit=5,
        )
        search_cmd = [c for c in self.backend.conn().commands if c[0] == "SEARCH"][-1]
        joined = b" ".join(a if isinstance(a, bytes) else str(a).encode()
                           for a in search_cmd[1:])
        self.assertIn(b"UNSEEN", joined)
        self.assertIn(b'FROM "bob@x.com"', joined)
        self.assertIn(b'SUBJECT "invoice"', joined)
        self.assertIn(b"SINCE 01-Jan-2026", joined)

    def test_message_id_round_trip(self):
        folder, uid = self.backend_imap.split_id("INBOX|103")
        self.assertEqual((folder, uid), ("INBOX", 103))
        folder, uid = self.backend_imap.split_id("Work/Sub|7")
        self.assertEqual((folder, uid), ("Work/Sub", 7))

    def test_bad_message_id_is_explained(self):
        with self.assertRaises(self.backend_imap.ImapError) as ctx:
            self.backend_imap.split_id("12345")
        self.assertIn("mail_search", str(ctx.exception))

    def test_build_message_sets_reply_headers(self):
        msg = self.backend.build_message(
            to=["bob@example.com"], subject="Re: hi", body="hello",
            in_reply_to="<abc123@example.com>", references="<root@example.com>",
        )
        self.assertEqual(msg["In-Reply-To"], "<abc123@example.com>")
        self.assertIn("<root@example.com>", msg["References"])
        self.assertIn("<abc123@example.com>", msg["References"])
        self.assertEqual(msg["From"], "me@icloud.com")

    def test_login_uses_configured_password(self):
        self.backend.conn()
        self.assertIn(("login", "me@icloud.com"), self.backend.conn().commands)


class TestImapWireFormat(unittest.TestCase):
    """Regressions for defects that only show up against a real IMAP server."""

    def setUp(self):
        import backend_imap
        from config import Account

        self.backend_imap = backend_imap
        self._real = backend_imap.imaplib.IMAP4_SSL
        backend_imap.imaplib.IMAP4_SSL = FakeImap
        self.backend = backend_imap.ImapBackend(Account({
            "name": "icloud", "kind": "imap", "provider": "icloud",
            "email": "me@icloud.com", "password": "pw",
        }))

    def tearDown(self):
        self.backend_imap.imaplib.IMAP4_SSL = self._real

    def test_mailbox_names_with_spaces_are_quoted(self):
        # imaplib does not quote arguments, so an unquoted "Sent Messages"
        # reaches the server as two atoms and is rejected with BAD. iCloud and
        # Gmail both use spaces in their default folder names.
        self.assertEqual(self.backend_imap._mbox("Sent Messages"), b'"Sent Messages"')
        self.assertEqual(self.backend_imap._mbox("[Gmail]/All Mail"), b'"[Gmail]/All Mail"')
        self.assertEqual(self.backend_imap._mbox("INBOX"), b'"INBOX"')
        self.assertEqual(self.backend_imap._mbox("已发送"), b'"&XfJT0ZAB-"')
        self.assertEqual(self.backend_imap._mbox('od"d'), b'"od\\"d"')

    def test_select_passes_a_quoted_mailbox(self):
        self.backend._select("Sent Messages")
        self.assertEqual(self.backend.conn().selected, b'"Sent Messages"')

    def test_move_passes_a_quoted_mailbox(self):
        self.backend.move(["INBOX|1"], "trash")
        move = [c for c in self.backend.conn().commands if c[0] == "MOVE"][-1]
        self.assertEqual(move[2], '"Deleted Messages"')

    def test_imap_id_command_is_registered(self):
        # imaplib has no ID command, so _simple_command('ID', ...) raises
        # KeyError before writing anything; 163/126/QQ then refuse to log in.
        self.assertIn("ID", self.backend_imap.imaplib.Commands)

    def test_id_command_is_actually_sent_for_163(self):
        from config import Account

        backend = self.backend_imap.ImapBackend(Account({
            "name": "163", "kind": "imap", "provider": "163",
            "email": "me@163.com", "password": "pw",
        }))
        backend.conn()
        self.assertTrue(
            any(c[0] == "ID" for c in backend.conn().commands),
            "the IMAP ID command was never sent",
        )

    def test_expunge_targets_only_the_given_uids(self):
        # A bare EXPUNGE erases every \\Deleted message in the folder, including
        # ones another client marked. With UIDPLUS we must scope it by UID.
        self.backend.delete(["INBOX|101", "INBOX|102"], permanent=True)
        conn = self.backend.conn()
        self.assertIn(("EXPUNGE", "101,102"), conn.commands)
        self.assertEqual(conn.plain_expunges, 0)

    def test_expunge_falls_back_without_uidplus(self):
        self.backend.conn().capabilities = ("IMAP4REV1",)
        self.backend.delete(["INBOX|101"], permanent=True)
        self.assertEqual(self.backend.conn().plain_expunges, 1)

    def test_move_falls_back_to_copy_when_server_rejects_move(self):
        conn = self.backend.conn()
        conn.fail_move = True
        result = self.backend.move(["INBOX|101"], "trash")
        self.assertEqual(result["moved"], 1)
        self.assertTrue(any(c[0] == "COPY" for c in conn.commands))

    def test_noselect_flag_is_case_insensitive(self):
        # QQ sends \NoSelect (mixed case); RFC 3501 flags are case-insensitive,
        # and an unselectable container leaking into the list breaks searches.
        conn = self.backend.conn()
        conn.list = lambda: ("OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\NoSelect \\HasChildren) "/" "Folders"',
        ])
        folders = self.backend.list_folders(refresh=True)
        self.assertEqual({f["name"] for f in folders}, {"INBOX"})

    def test_reversible_delete_refuses_without_a_trash_folder(self):
        # permanent=False on a server with no detectable Trash must refuse, not
        # silently escalate into an unconfirmed expunge.
        self.backend._folder_cache = [
            {"name": "INBOX", "role": "inbox", "flags": [], "role_from_flag": True},
        ]
        with self.assertRaises(self.backend_imap.ImapError) as ctx:
            self.backend.delete(["INBOX|101"], permanent=False)
        self.assertIn("Trash", str(ctx.exception))
        conn = self.backend.conn()
        self.assertEqual(conn.plain_expunges, 0)
        self.assertFalse(any(c[0] in ("STORE", "EXPUNGE") for c in conn.commands))


class TestGraphQueryBuilding(unittest.TestCase):
    """Graph rejects several query shapes outright; check we never emit them."""

    def setUp(self):
        from backend_graph import GraphBackend
        from config import Account

        self.backend = GraphBackend(Account({
            "name": "outlook", "kind": "graph", "email": "me@outlook.com",
            "client_id": "cid", "tenant": "consumers",
        }))
        self.calls = []

        def fake_request(method, path, body=None, params=None, raw_response=False):
            self.calls.append({"method": method, "path": path, "params": params or {}})
            return {"value": []}

        self.backend._request = fake_request
        self.backend._folders = []
        self.backend._folder_names = {}

    def test_ids_are_fully_percent_encoded(self):
        from backend_graph import _qid

        # Graph ids are base64-flavoured; '/' must not survive into the path.
        self.assertEqual(_qid("AAMk=/foo+bar"), "AAMk%3D%2Ffoo%2Bbar")
        self.assertNotIn("/", _qid("a/b/c"))

    def test_message_path_encodes_the_id(self):
        self.backend.get_message("AAMk=/abc+def", include_body=False)
        self.assertIn("%2F", self.calls[0]["path"])
        self.assertNotIn("AAMk=/abc", self.calls[0]["path"])

    def test_search_does_not_nest_raw_quotes(self):
        self.backend.search(query="invoice", sender="alice@example.com", limit=5)
        search = self.calls[-1]["params"]["$search"]
        self.assertTrue(search.startswith('"') and search.endswith('"'))
        # No unescaped inner quote would make this invalid KQL.
        self.assertNotIn('"', search[1:-1].replace('\\"', ""))
        self.assertIn("from:alice@example.com", search)

    def test_multiword_kql_values_are_quoted(self):
        self.backend.search(subject="tax receipt 2026", limit=5)
        search = self.calls[-1]["params"]["$search"]
        self.assertIn('subject:\\"tax receipt 2026\\"', search)

    def test_search_and_filter_are_never_combined(self):
        self.backend.search(query="hello", unread_only=True, since="2026-01-01", limit=5)
        params = self.calls[-1]["params"]
        self.assertIn("$search", params)
        self.assertNotIn("$filter", params)
        self.assertNotIn("$orderby", params)

    def test_filter_branch_never_uses_contains_or_lambdas(self):
        # Graph does not support contains() or any() on messages.
        self.backend.search(unread_only=True, has_attachment=True,
                            since="2026-01-01", limit=5)
        params = self.calls[-1]["params"]
        self.assertNotIn("$search", params)
        self.assertNotIn("contains(", params.get("$filter", ""))
        self.assertNotIn("/any(", params.get("$filter", ""))
        self.assertIn("isRead eq false", params["$filter"])
        self.assertIn("$orderby", params)

    def test_sender_and_subject_route_through_search(self):
        for kwargs in ({"sender": "bank@x.com"}, {"subject": "invoice"},
                       {"recipient": "me@x.com"}):
            with self.subTest(**kwargs):
                self.calls.clear()
                self.backend.search(limit=5, **kwargs)
                self.assertIn("$search", self.calls[-1]["params"])

    def test_folders_are_expanded_recursively(self):
        # v1.0 rejects multi-level $expand, so nesting is walked with explicit
        # childFolders requests instead.
        def fake_request(method, path, body=None, params=None, raw_response=False):
            self.calls.append({"method": method, "path": path, "params": params or {}})
            if path == "/me/mailFolders/inbox":
                return {"id": "f1"}
            if path.endswith("/childFolders"):
                return {"value": [{"id": "f2", "displayName": "Receipts",
                                   "unreadItemCount": 0, "totalItemCount": 4,
                                   "childFolderCount": 0}]}
            if path == "/me/mailFolders":
                return {"value": [{"id": "f1", "displayName": "Inbox",
                                   "unreadItemCount": 3, "totalItemCount": 10,
                                   "childFolderCount": 1}]}
            return {"value": []}  # other well-known probes: no such folder

        self.backend._request = fake_request
        folders = self.backend.list_folders(refresh=True)
        names = {f["name"] for f in folders}
        self.assertEqual(names, {"Inbox", "Inbox/Receipts"})
        by_name = {f["name"]: f for f in folders}
        self.assertEqual(by_name["Inbox"]["role"], "inbox")
        # A subfolder must be addressable by both its path and its bare name.
        self.assertEqual(self.backend._resolve_folder("Inbox/Receipts"), "f2")
        self.assertEqual(self.backend._resolve_folder("Receipts"), "f2")

    def test_get_thread_never_orders_a_conversation_filter(self):
        # $filter conversationId eq ... combined with $orderby answers
        # 400 InefficientFilter on the live API; sort must happen client-side.
        def fake_request(method, path, body=None, params=None, raw_response=False):
            self.calls.append({"method": method, "path": path, "params": params or {}})
            if path.startswith("/me/messages/"):
                return {"conversationId": "conv1"}
            return {"value": []}

        self.backend._request = fake_request
        self.backend.get_thread("mid", limit=10)
        listing = [c for c in self.calls if c["path"] == "/me/messages"][-1]
        self.assertIn("$filter", listing["params"])
        self.assertNotIn("$orderby", listing["params"])

    def test_folder_listing_stays_on_v1_grammar(self):
        # wellKnownName and $expand=childFolders($levels=...) exist only on the
        # beta endpoint; on v1.0 they answer 400, which used to break every
        # Graph folder listing (mail_list_folders and mail_digest).
        self.backend.list_folders(refresh=True)
        for call in self.calls:
            blob = json.dumps(call)
            self.assertNotIn("wellKnownName", blob)
            self.assertNotIn("$levels", blob)

    def test_no_dollar_prefixed_unknown_options(self):
        self.backend.list_folders(refresh=True)
        params = self.calls[-1]["params"]
        known = {"$top", "$select", "$expand", "$filter", "$orderby", "$search", "$count"}
        for key in params:
            if key.startswith("$"):
                self.assertIn(key, known, f"unknown OData option {key}")


class TestGraphUploadSession(unittest.TestCase):
    """>3 MB Graph attachments must go through chunked upload sessions."""

    def setUp(self):
        import backend_graph
        from config import Account

        self.backend_graph = backend_graph
        self.backend = backend_graph.GraphBackend(Account({
            "name": "outlook", "kind": "graph", "email": "me@outlook.com",
            "client_id": "cid", "tenant": "consumers",
        }))

    def test_large_attachment_uses_chunked_upload(self):
        calls, puts = [], []

        def fake_request(method, path, body=None, params=None, raw_response=False):
            calls.append({"method": method, "path": path, "body": body})
            return {"uploadUrl": "https://upload.example/session1"}

        def fake_put(upload_url, chunk, offset, total):
            puts.append({"url": upload_url, "len": len(chunk),
                         "offset": offset, "total": total})

        self.backend._request = fake_request
        self.backend._put_chunk = fake_put

        size = 7 * 1024 * 1024 + 123
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "big.bin")
            with open(path, "wb") as fh:
                fh.write(b"\x5a" * size)
            self.backend._attach("MSG", path)

        create = calls[-1]
        self.assertIn("createUploadSession", create["path"])
        self.assertEqual(create["body"]["AttachmentItem"]["size"], size)

        # Chunks must be contiguous, cover the file exactly, and every chunk
        # except the last must be a multiple of 320 KiB (Graph requirement).
        self.assertGreater(len(puts), 1)
        expected_offset = 0
        for i, put in enumerate(puts):
            self.assertEqual(put["offset"], expected_offset)
            self.assertEqual(put["total"], size)
            if i < len(puts) - 1:
                self.assertEqual(put["len"] % 327_680, 0)
            expected_offset += put["len"]
        self.assertEqual(expected_offset, size)

    def test_small_attachment_stays_on_simple_path(self):
        calls = []

        def fake_request(method, path, body=None, params=None, raw_response=False):
            calls.append({"path": path})
            return {}

        self.backend._request = fake_request
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "small.txt")
            with open(path, "wb") as fh:
                fh.write(b"x" * 1024)
            self.backend._attach("MSG", path)
        self.assertNotIn("createUploadSession", calls[-1]["path"])

    def test_oversize_attachment_is_refused(self):
        real = self.backend_graph._UPLOAD_SESSION_MAX
        self.backend_graph._UPLOAD_SESSION_MAX = 1024
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "huge.bin")
                with open(path, "wb") as fh:
                    fh.write(b"x" * 2048)
                with self.assertRaises(self.backend_graph.GraphError):
                    self.backend._attach("MSG", path)
        finally:
            self.backend_graph._UPLOAD_SESSION_MAX = real


class TestBundledCredentials(unittest.TestCase):
    """The cloud/portable fallback: a .mailbridge/ dir shipped in the bundle."""

    def test_env_override_wins(self):
        import config

        self.assertEqual(config.resolve_config_dir("/custom/dir"), "/custom/dir")

    def test_home_beats_bundle_and_bundle_beats_default(self):
        import config

        real_expand = config.os.path.expanduser
        real_bundle = config._BUNDLED_DIR
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = os.path.join(tmp, "home")
            os.makedirs(fake_home)
            home_cfg = os.path.join(fake_home, ".mailbridge")
            bundle = os.path.join(tmp, "plugin", ".mailbridge")
            config.os.path.expanduser = lambda p: fake_home
            config._BUNDLED_DIR = bundle
            try:
                # Nothing configured anywhere -> default home path.
                self.assertEqual(config.resolve_config_dir(None), home_cfg)
                # Bundled credentials exist, home does not -> bundle wins.
                os.makedirs(bundle)
                with open(os.path.join(bundle, "accounts.json"), "w") as fh:
                    fh.write("{}")
                self.assertEqual(config.resolve_config_dir(None), bundle)
                # Home config appears -> home wins over the bundle.
                os.makedirs(home_cfg)
                with open(os.path.join(home_cfg, "accounts.json"), "w") as fh:
                    fh.write("{}")
                self.assertEqual(config.resolve_config_dir(None), home_cfg)
            finally:
                config.os.path.expanduser = real_expand
                config._BUNDLED_DIR = real_bundle


class TestBodyExtraction(unittest.TestCase):
    def test_multipart_alternative_prefers_plain_text(self):
        import email
        from backend_imap import _extract_body

        raw = (
            "MIME-Version: 1.0\r\n"
            'Content-Type: multipart/alternative; boundary="B"\r\n\r\n'
            "--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            "Plain version\r\n"
            "--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
            "<p>HTML version</p>\r\n--B--\r\n"
        ).encode()
        body = _extract_body(email.message_from_bytes(raw))
        self.assertEqual(body["body"], "Plain version")
        self.assertEqual(body["body_format"], "text")

    def test_html_only_is_converted(self):
        import email
        from backend_imap import _extract_body

        raw = (
            "MIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
            "<p>Hello <b>there</b></p>"
        ).encode()
        body = _extract_body(email.message_from_bytes(raw))
        self.assertEqual(body["body"], "Hello there")
        self.assertEqual(body["body_format"], "html->text")

    def test_attachments_are_listed_not_inlined(self):
        import base64
        import email
        from backend_imap import _extract_body

        blob = base64.b64encode(b"PDFDATA" * 10).decode()
        raw = (
            "MIME-Version: 1.0\r\n"
            'Content-Type: multipart/mixed; boundary="M"\r\n\r\n'
            "--M\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nSee attached.\r\n"
            "--M\r\nContent-Type: application/pdf; name=\"invoice.pdf\"\r\n"
            'Content-Disposition: attachment; filename="invoice.pdf"\r\n'
            f"Content-Transfer-Encoding: base64\r\n\r\n{blob}\r\n--M--\r\n"
        ).encode()
        body = _extract_body(email.message_from_bytes(raw))
        self.assertEqual(body["body"], "See attached.")
        self.assertTrue(body["has_attachments"])
        self.assertEqual(len(body["attachments"]), 1)
        self.assertEqual(body["attachments"][0]["filename"], "invoice.pdf")
        self.assertEqual(body["attachments"][0]["content_type"], "application/pdf")
        self.assertEqual(body["attachments"][0]["id"], "0")

    def test_gb18030_body_decodes(self):
        import email
        from backend_imap import _extract_body

        raw = (
            "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=gb18030\r\n\r\n"
        ).encode() + "中文正文".encode("gb18030")
        body = _extract_body(email.message_from_bytes(raw))
        self.assertEqual(body["body"], "中文正文")


class TestSubjectNormalisation(unittest.TestCase):
    def test_prefixes_are_stripped(self):
        sys.path.insert(0, SERVER_DIR)
        import server as srv

        for raw, expected in [
            ("Re: Fwd: Project update", "Project update"),
            ("RE: RE: RE: Invoice", "Invoice"),
            ("回复: 项目进展", "项目进展"),
            ("转发：合同", "合同"),
            ("Re[2]: Budget", "Budget"),
            ("No prefix here", "No prefix here"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(srv._normalise_subject(raw), expected)


class TestConfig(unittest.TestCase):
    def test_missing_password_explains_app_passwords(self):
        from config import Account, ConfigError

        acct = Account({"name": "i", "kind": "imap", "provider": "icloud",
                        "email": "a@icloud.com"})
        with self.assertRaises(ConfigError) as ctx:
            _ = acct.password
        self.assertIn("app-specific password", str(ctx.exception))

    def test_missing_client_id_explains_registration(self):
        from config import Account, ConfigError

        os.environ.pop("MAILBRIDGE_MS_CLIENT_ID", None)
        acct = Account({"name": "o", "kind": "graph", "email": "a@outlook.com"})
        with self.assertRaises(ConfigError) as ctx:
            _ = acct.client_id
        self.assertIn("Entra", str(ctx.exception))

    def test_password_from_env(self):
        from config import Account

        os.environ["TEST_MB_PW"] = "secret123"
        acct = Account({"name": "i", "kind": "imap", "provider": "gmail",
                        "email": "a@gmail.com", "password_env": "TEST_MB_PW"})
        self.assertEqual(acct.password, "secret123")
        del os.environ["TEST_MB_PW"]

    def test_provider_presets_applied(self):
        from config import Account

        acct = Account({"name": "q", "kind": "imap", "provider": "qq",
                        "email": "a@qq.com", "password": "x"})
        self.assertEqual(acct.imap_host, "imap.qq.com")
        self.assertEqual(acct.smtp_port, 465)
        self.assertEqual(acct.smtp_mode, "ssl")
        self.assertTrue(acct.needs_imap_id)

    def test_duplicate_names_rejected(self):
        from config import Config, ConfigError

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"accounts": [
                {"name": "a", "kind": "imap", "email": "x@y.com"},
                {"name": "a", "kind": "imap", "email": "z@y.com"},
            ]}, fh)
            path = fh.name
        try:
            with self.assertRaises(ConfigError) as ctx:
                Config(path)
            self.assertIn("Duplicate", str(ctx.exception))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
