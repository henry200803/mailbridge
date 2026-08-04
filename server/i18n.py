"""Bilingual (English / 简体中文) string catalog for mailbridge.

Language resolution, in priority order:
  1. the MAILBRIDGE_LANG environment variable ("en" or "zh")
  2. the "language" key at the top level of accounts.json
  3. default: "en"

Design: English strings live in the source as-is and double as catalog keys,
so a missing translation degrades to English instead of crashing. `t()`
translates wizard/UI strings; `localize_server()` swaps MCP tool descriptions
and instructions in place after registration.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

# Kept in sync with config.py (not imported, to avoid a dependency cycle):
# MAILBRIDGE_HOME env > ~/.mailbridge (when configured) > bundled .mailbridge/.
_BUNDLED_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, ".mailbridge")
)


def _config_path() -> str:
    explicit = os.environ.get("MAILBRIDGE_CONFIG")
    if explicit:
        return explicit
    base = os.environ.get("MAILBRIDGE_HOME")
    if not base:
        home = os.path.join(os.path.expanduser("~"), ".mailbridge")
        if os.path.exists(os.path.join(home, "accounts.json")):
            base = home
        elif os.path.exists(os.path.join(_BUNDLED_DIR, "accounts.json")):
            base = _BUNDLED_DIR
        else:
            base = home
    return os.path.join(base, "accounts.json")


_lang: Optional[str] = None


def get_lang() -> str:
    """Return the active language code: 'en' or 'zh'."""
    global _lang
    if _lang is None:
        value = os.environ.get("MAILBRIDGE_LANG", "").strip().lower()
        if not value:
            try:
                with open(_config_path(), "r", encoding="utf-8") as fh:
                    value = str(json.load(fh).get("language") or "").strip().lower()
            except (OSError, json.JSONDecodeError, AttributeError):
                value = ""
        _lang = "zh" if value.startswith("zh") else "en"
    return _lang


def reset_lang() -> None:
    """Forget the cached language (config may have changed)."""
    global _lang
    _lang = None


def t(text: str, **fmt: Any) -> str:
    """Translate a UI string. Falls back to the English original."""
    if get_lang() == "zh":
        text = _ZH_UI.get(text, text)
    return text.format(**fmt) if fmt else text


def provider_hint(provider: str, default: str) -> str:
    """Localized app-password hint for an IMAP provider."""
    if get_lang() == "zh":
        return _ZH_PROVIDER_HINTS.get(provider, _ZH_PROVIDER_HINTS.get("generic", default))
    return default


def localize_server(server: Any) -> None:
    """Swap tool descriptions and instructions to the active language.

    Call once after every tool is registered and before serving. English is
    the source of truth; this only applies zh overrides that exist.
    """
    if get_lang() != "zh":
        return
    server.instructions = _ZH_INSTRUCTIONS
    for name, tool in getattr(server, "_tools", {}).items():
        override = _ZH_TOOLS.get(name)
        if override:
            tool.description = override
        props = tool.input_schema.get("properties", {})
        for prop, spec in props.items():
            zh = _ZH_TOOL_PROPS.get((name, prop)) or _ZH_PROPS.get(prop)
            if zh and "description" in spec:
                # Copy before mutating: shared schema constants (ACCOUNT_PROP
                # etc.) are referenced by several tools, and an in-place write
                # would leak a tool-specific override into all of them.
                props[prop] = {**spec, "description": zh}


# ---------------------------------------------------------------- MCP surface

_ZH_INSTRUCTIONS = """\
mailbridge 提供用户所有已配置邮箱的完整读写能力。

消息 id：IMAP 的 id 形如 "INBOX|48213"（文件夹|uid），Graph 的 id 是一长串不透明
字符串。id 一律来自 mail_search 的结果——绝不要凭空构造；如果某次调用报告 id 已
失效，请重新搜索获取最新 id。

高效使用：
  * mail_search 只返回摘要。需要读正文时再调 mail_get_message，且只读真正相关的。
  * 不清楚有哪些账号时，先调 mail_list_accounts。
  * mail_search 省略 `accounts` 参数即可同时搜索所有已配置账号。
  * "有什么需要我处理的" 这类需求用 mail_digest 一次拿到全量摘要，不要发起多次搜索。

发送邮件或永久删除之前，必须把将要执行的内容展示给用户并获得明确同意。这两个工具
同时要求 confirm=true。
"""

_ZH_TOOLS: Dict[str, str] = {
    "mail_list_accounts": (
        "列出全部已配置邮箱及其类型与连接状态。不清楚有哪些账号时先调用此工具。"
    ),
    "mail_list_folders": (
        "列出某个邮箱的文件夹（提供商支持时附带未读/总数统计）。移动邮件前用它确认"
        "目标文件夹的准确名称。"
    ),
    "mail_search": (
        "在一个或全部账号中搜索邮件，返回消息摘要（不含正文）。过滤条件可自由组合："
        "全文 `query`、`sender`、`subject`、日期范围、仅未读、仅含附件。结果按时间"
        "倒序。用返回的 `id` 配合 mail_get_message 读取全文。"
    ),
    "mail_get_message": (
        "读取一封邮件的完整内容：头信息、解码后的正文和附件列表。HTML 邮件会转换为"
        "可读纯文本。"
    ),
    "mail_get_thread": (
        "获取一封邮件所在的完整会话，按时间从旧到新。Graph 账号使用真实会话 id；"
        "IMAP 账号在收件箱与已发送中按规范化主题匹配。"
    ),
    "mail_download_attachment": (
        "下载一个附件到本地目录并返回保存路径。先用 mail_get_message 获取附件的 id "
        "或文件名。"
    ),
    "mail_digest": (
        "汇总所有账号的待处理邮件：各文件夹未读计数加上最新的未读摘要。「有什么需要"
        "我处理」或每日收件箱回顾用这一个调用完成，不要发起多次搜索。"
    ),
    "mail_export": (
        "执行一次搜索并把完整结果集写入磁盘上的 CSV 或 JSON 文件。批量任务（某纳税"
        "年度的所有收据、某发件人的全部邮件、附件清单）用它，避免把几百封邮件拉进"
        "上下文。"
    ),
    "mail_draft": (
        "在邮箱中保存草稿而不发送。这是准备回复最稳妥的方式——用户在邮件客户端里"
        "自行审阅并发送。传入 reply_to_message_id 可保持草稿与原会话的串联。"
    ),
    "mail_send": (
        "立即发送一封邮件。要求 confirm=true。调用前必须向用户展示准确的收件人、"
        "主题和正文并获得明确同意——有任何疑虑时优先使用 mail_draft。"
    ),
    "mail_mark": "批量标记邮件的已读/未读、星标/取消星标状态。",
    "mail_move": (
        "把邮件移动到其他文件夹。先用 mail_list_folders 确认目标名称，或直接传入 "
        "'archive'、'trash' 这类角色名。"
    ),
    "mail_delete": (
        "删除邮件。默认移入回收站，可恢复。permanent=true 为不可逆的彻底删除，且"
        "额外要求 confirm=true——务必先列出将被删除的内容并获得用户明确同意。"
    ),
    "mail_auth_status": (
        "检查 Microsoft 账号（个人 Outlook.com 与工作/学校 Exchange）的 OAuth 状态："
        "是否已授权、缓存令牌能否刷新，未授权时给出精确的修复命令。"
    ),
}

_ZH_PROPS: Dict[str, str] = {
    "account": "账号名，来自 mail_list_accounts。省略则使用默认账号。",
    "accounts": "要包含的账号名列表。省略则搜索所有已配置账号。",
    "folder": (
        "目标文件夹。接受真实文件夹名，或 'inbox'、'sent'、'drafts'、'trash'、"
        "'junk'、'archive' 这类角色名。默认收件箱。"
    ),
    "message_ids": "来自 mail_search 结果的消息 id 列表。",
    "message_id": "来自 mail_search 的消息 id。",
    "query": "对整封邮件（主题、正文、头信息）的全文搜索。",
    "sender": "匹配发件人地址或显示名。",
    "recipient": "匹配收件人地址。",
    "subject": "匹配主题中的文本。",
    "since": "只要这个日期当天及之后的邮件（YYYY-MM-DD 或 ISO-8601）。",
    "before": "只要严格早于这个日期的邮件（YYYY-MM-DD 或 ISO-8601）。",
    "unread_only": "仅未读邮件。",
    "flagged_only": "仅星标邮件。",
    "has_attachment": "仅含附件的邮件。",
    "to": "收件人地址列表。",
    "cc": "抄送地址列表。",
    "bcc": "密送地址列表。",
    "body": "纯文本正文。",
    "html_body": "可选的 HTML 正文。",
    "reply_to_message_id": "被回复邮件的 id，用于保持会话串联。",
    "attachments": "要附加的本地文件路径列表。",
    "read": "true = 标为已读，false = 标为未读。",
    "flagged": "true = 加星标，false = 取消星标。",
    "destination": "目标文件夹名或角色名。",
    "permanent": "跳过回收站直接彻底删除。不可逆。",
    "with_counts": "同时获取每个文件夹的未读/总数（IMAP 上较慢）。",
    "max_body_chars": "正文超过此字符数时截断，保护上下文。",
    "dest_dir": "保存目录。默认 ~/Downloads/mailbridge。",
    "attachment": "附件的 `id` 或 `filename`，来自 mail_get_message。",
    "out_path": "输出文件路径。扩展名决定格式（.csv 或 .json）。",
    "include_bodies": "同时抓取并包含每封邮件的正文。明显更慢。",
    "per_account_limit": "每个账号返回多少条未读摘要。",
}

_ZH_TOOL_PROPS: Dict[tuple, str] = {
    ("mail_search", "limit"): "每个账号的最大结果数。",
    ("mail_get_thread", "message_id"): "会话中的任意一封邮件的 id。",
    ("mail_get_thread", "limit"): "会话内最多返回多少封邮件。",
    ("mail_export", "limit"): "每个账号最多导出多少封邮件。",
    ("mail_digest", "since"): "只统计这个日期当天及之后的邮件（YYYY-MM-DD）。",
    ("mail_send", "confirm"): "必须为 true。只有在用户确认过这封邮件的准确内容后才能设置。",
    ("mail_delete", "confirm"): "permanent=true 时必需。",
}

# ---------------------------------------------------------- provider hints

_ZH_PROVIDER_HINTS: Dict[str, str] = {
    "icloud": (
        "iCloud 需要 App 专用密码：在 account.apple.com > 登录与安全 > App 专用密码 "
        "生成。你的 Apple ID 登录密码在这里不可用。"
    ),
    "gmail": (
        "Gmail 需要应用专用密码（必须先开启两步验证）：myaccount.google.com > 安全性 "
        "> 两步验证 > 应用专用密码。"
    ),
    "qq": (
        "QQ 邮箱需要授权码而不是 QQ 密码：QQ 邮箱网页版 设置 > 账户 > IMAP/SMTP服务 "
        "> 开启 后生成。"
    ),
    "163": "163 邮箱需要授权码而不是登录密码：设置 > POP3/SMTP/IMAP 中开启并生成。",
    "126": "126 邮箱需要授权码（在邮箱设置中开启 IMAP 服务时生成）。",
    "yahoo": "Yahoo 需要在账号安全设置中生成应用密码。",
    "fastmail": "Fastmail 需要在 Settings > Privacy & Security 中生成应用密码。",
    "generic": "使用你的邮件服务商要求的密码或应用专用密码。",
}

# ------------------------------------------------------------- wizard strings

_ZH_UI: Dict[str, str] = {
    # Menu / commands
    "mailbridge setup": "mailbridge 配置向导",
    "Add a mailbox": "添加邮箱",
    "Sign in to a Microsoft account": "登录 Microsoft 账号",
    "Test a mailbox": "测试邮箱连接",
    "Set the default mailbox": "设置默认邮箱",
    "Remove a mailbox": "移除邮箱",
    "Language / 语言": "Language / 语言",
    "Quit": "退出",
    "Choose": "请选择",
    "Account name": "账号名",
    "Account name (blank for all)": "账号名（留空则全部）",
    "Cancelled.": "已取消。",
    "Bye.": "再见。",
    # Add flow
    "Which provider? (number)": "选择服务商（输入序号）",
    "Enter a number from the list.": "请输入列表中的序号。",
    "Email address": "邮箱地址",
    "That does not look like an email address.": "这看起来不是一个有效的邮箱地址。",
    "Short name for this account (used in conversation)": "账号简称（对话中使用）",
    "An account named '{name}' already exists.": "已存在名为 '{name}' 的账号。",
    "Display name to send mail as (optional)": "发信显示名（可选）",
    "A client ID is required for Microsoft accounts.": "Microsoft 账号必须提供 client ID。",
    "Application (client) ID": "应用程序（客户端）ID",
    "Tenant (press enter for 'organizations', or paste your tenant domain/GUID if you know it)":
        "租户（直接回车使用 'organizations'，或粘贴你的租户域名/GUID）",
    "IMAP host (e.g. mail.example.com)": "IMAP 服务器（如 mail.example.com）",
    "IMAP port": "IMAP 端口",
    "SMTP host": "SMTP 服务器",
    "SMTP port": "SMTP 端口",
    "SMTP mode (starttls or ssl)": "SMTP 模式（starttls 或 ssl）",
    "Password / app password (input is hidden)": "密码 / 应用专用密码（输入不回显）",
    "A password is required.": "必须输入密码。",
    "Saved '{name}' to {path}": "已将 '{name}' 保存到 {path}",
    "Sign in to Microsoft now?": "现在登录 Microsoft 吗？",
    "Test the connection now?": "现在测试连接吗？",
    # List / status
    "Configured mailboxes": "已配置的邮箱",
    "No accounts configured yet. Run: python3 setup.py add":
        "尚未配置任何账号。运行：python3 setup.py add",
    "* = default account": "* = 默认账号",
    "authorised": "已授权",
    "not authorised — run: python3 setup.py auth {name}":
        "未授权 — 运行：python3 setup.py auth {name}",
    # Auth flow
    "'{name}' is an IMAP account — it uses a password, not OAuth.":
        "'{name}' 是 IMAP 账号——它使用密码而不是 OAuth。",
    "Sign in to Microsoft": "登录 Microsoft",
    "  1. Open  {url}": "  1. 打开  {url}",
    "  2. Enter the code  {code}": "  2. 输入代码  {code}",
    "  3. Sign in as  {email}  and approve the permissions.":
        "  3. 用  {email}  登录并同意权限请求。",
    "Waiting for you to finish… (Ctrl+C to cancel)": "等待你完成登录…（Ctrl+C 取消）",
    "Authorised. Token cached at {path}": "授权成功。令牌已缓存到 {path}",
    # Test flow
    "Testing '{name}' ({email})": "正在测试 '{name}'（{email}）",
    "folders": "文件夹",
    "{n} found": "共 {n} 个",
    "not detected": "未识别",
    "{n} recent message(s) readable": "可读取 {n} 封最近邮件",
    "'{name}' is working.": "'{name}' 工作正常。",
    "Failed:": "失败：",
    "No accounts configured.": "尚未配置任何账号。",
    # Remove / default
    "Remove '{name}' from the config?": "从配置中移除 '{name}' 吗？",
    "No account named '{name}'.": "不存在名为 '{name}' 的账号。",
    "Cached token deleted.": "已删除缓存的令牌。",
    "Removed '{name}'.": "已移除 '{name}'。",
    "Default account is now '{name}'.": "默认账号现在是 '{name}'。",
    # Language
    "lang must be 'en' or 'zh'": "语言只能是 'en' 或 'zh'",
    "Current language: {lang}": "当前语言：{lang}",
    "Switch to (en/zh)": "切换到（en/zh）",
    "Language saved: {lang}": "语言已保存：{lang}",
    "Restart your MCP client for tool descriptions to switch.":
        "重启 MCP 客户端后工具描述才会切换语言。",
    # Providers
    "iCloud Mail": "iCloud 邮箱",
    "Outlook.com / Hotmail / Live (personal)": "Outlook.com / Hotmail / Live（个人）",
    "Work or school Microsoft account (Exchange Online, e.g. a university)":
        "工作或学校 Microsoft 账号（Exchange Online，如大学邮箱）",
    "Gmail": "Gmail",
    "QQ Mail": "QQ 邮箱",
    "163 Mail": "163 邮箱",
    "126 Mail": "126 邮箱",
    "Yahoo Mail": "Yahoo 邮箱",
    "Fastmail": "Fastmail",
    "Other (custom IMAP/SMTP host)": "其他（自定义 IMAP/SMTP 服务器）",
}

_ZH_AZURE_HELP = """\
访问 Microsoft 邮箱需要一个免费的应用注册。这是一次性的五分钟工作，完全免费。

  1. 打开  https://entra.microsoft.com  用任意 Microsoft 账号登录。
  2. 左侧栏：Applications > App registrations >「New registration」。
  3. 名称填  mailbridge （随意）。
  4. Supported account types 选择
        「Accounts in any organizational directory and personal Microsoft accounts」
     这一项同时覆盖 Outlook.com 个人邮箱和工作/学校邮箱。
  5. Redirect URI 留空，点 Register。
  6. 在概览页复制「Application (client) ID」——即下面要粘贴的值。
  7. 新应用左侧栏：Authentication，滚动到 Advanced settings，把
     「Allow public client flows」设为  Yes  并保存。
  8. 左侧栏：API permissions > Add a permission > Microsoft Graph >
     Delegated permissions，添加  Mail.ReadWrite、Mail.Send、User.Read、
     offline_access，然后点「Add permissions」。

工作/学校账号的注意事项：很多机构（包括大学）会拦截未经 IT 部门批准的第三方应用。
如果之后登录报 consent 错误，需要租户管理员批准——那是对方的政策决定，应用本身
无法绕过。
"""


def azure_help(default: str) -> str:
    return _ZH_AZURE_HELP if get_lang() == "zh" else default
