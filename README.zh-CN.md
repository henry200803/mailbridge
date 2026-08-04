<div align="center">

# 📮 mailbridge

**邮件 MCP server —— 让 Claude、Codex、Cursor 或任何 AI Agent 拥有真正的邮件客户端：iCloud、Outlook、Gmail、QQ、163 及任意 IMAP 邮箱。**

[English](README.md) | **简体中文**

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Dependencies](https://img.shields.io/badge/依赖-零-brightgreen)
![Protocol](https://img.shields.io/badge/协议-MCP-8A2BE2)
![License](https://img.shields.io/badge/许可证-MIT-green)
![i18n](https://img.shields.io/badge/语言-EN%20%2F%20中文-orange)

</div>

在对话里跨所有邮箱搜索、阅读、起草、发送、归档、批量导出邮件。纯 Python 编写，**零第三方依赖**：不需要 `pip install`，没有构建步骤——只要有 Python 3.9+ 就能跑。

## 为什么做这个

多数 Agent 平台的内置连接器只覆盖 Gmail 和 Microsoft 365 工作账号，不覆盖个人 iCloud、个人 Outlook.com、QQ、163 或自建 IMAP 服务器——而且搜索连接器不等于邮件客户端。mailbridge 补上缺失的能力：附件、文件夹管理、真实发信、批量导出。它说的是标准 MCP（stdio），同一个 server 可以在 Claude、Codex、Cursor、Cline、Gemini CLI 等任何能启动 MCP server 的客户端里使用。

| 服务商 | 方式 | 凭据 |
|---|---|---|
| iCloud 邮箱 | IMAP/SMTP | App 专用密码 |
| Gmail | IMAP/SMTP | 应用专用密码 |
| QQ / 163 / 126 | IMAP/SMTP | 授权码 |
| Yahoo / Fastmail / 自定义 | IMAP/SMTP | 服务商应用密码 |
| Outlook.com / Hotmail | Microsoft Graph | 免费 Entra 应用注册（OAuth） |
| 工作 / 学校 Exchange | Microsoft Graph | 同一注册 + 租户同意 |

## 快速开始

```bash
git clone https://github.com/henry200803/mailbridge.git
python mailbridge/server/setup.py        # 交互式向导：添加账号、登录、测试
```

然后在任意 MCP 客户端注册这个 server：

```jsonc
// Claude Desktop (claude_desktop_config.json)、Cursor、Cline 等
{
  "mcpServers": {
    "mailbridge": {
      "command": "python",
      "args": ["/path/to/mailbridge/server/server.py"]
    }
  }
}
```

```bash
# Claude Code
claude mcp add mailbridge -- python /path/to/mailbridge/server/server.py
```

```bash
# Codex CLI
codex mcp add mailbridge -- python /path/to/mailbridge/server/server.py
```

Cowork 用户：直接通过插件安装器安装打包好的 `.zip` —— 内置的 `.mcp.json` 会自动完成接线。

## Microsoft 账号需要一个（免费的）client ID

Microsoft 已停用基于密码的 IMAP/SMTP，因此 Outlook.com / Hotmail 和工作/学校邮箱只能走 Microsoft Graph + OAuth——这需要一个 **Entra 应用注册**。大多数个人用户还没有：它完全免费（不需要 Azure 订阅），在浏览器里五分钟搞定，而且**一个注册覆盖你所有的 Microsoft 邮箱**。

按分步指南操作 —— **[为 mailbridge 注册 Microsoft 应用](skills/mail-setup/references/azure-app-registration.md)** —— 然后把得到的 *Application (client) ID* 粘贴进配置向导即可。指南还覆盖了租户取值、所有常见 `AADSTS` 错误码，以及大学邮箱的管理员同意问题。你的 client ID 只保存在本机的 `accounts.json` 里；它是你自己注册的配置项，本仓库不包含任何人的 client ID。

## 工具一览

| 工具 | 用途 |
|---|---|
| `mail_list_accounts` | 有哪些邮箱、是否工作正常 |
| `mail_list_folders` | 文件夹树及未读/总数统计 |
| `mail_search` | 单账号或全账号的组合条件搜索 |
| `mail_get_message` | 读取整封邮件，HTML 转纯文本 |
| `mail_get_thread` | 整个会话 |
| `mail_digest` | 一次调用完成跨账号未读盘点 |
| `mail_download_attachment` | 下载附件到磁盘 |
| `mail_export` | 批量导出搜索结果为 CSV / JSON |
| `mail_draft` | 保存草稿，可串联回复会话 |
| `mail_send` | 立即发送 —— 要求 `confirm=true` |
| `mail_mark` | 批量已读/未读、星标/取消 |
| `mail_move` | 移动邮件到文件夹 |
| `mail_delete` | 默认进回收站；彻底删除需 `confirm=true` |
| `mail_auth_status` | Microsoft 账号的 OAuth 健康状态 |

## 中英双语 🇬🇧🇨🇳

工具描述、配置向导和服务商提示同时提供英文与简体中文，随时切换：

```bash
python server/setup.py lang zh   # 或：lang en
```

也可以在 MCP 客户端配置里设置环境变量 `MAILBRIDGE_LANG=zh`。任何未翻译的字符串自动回退英文。

## 云端 / 便携模式

凭据默认保存在运行 server 那台机器的 `~/.mailbridge/`。在临时环境（云端沙箱、容器）中可以改为**随包携带**：把同样的 `.mailbridge/` 目录（`accounts.json` + `tokens/`）放到 `server/` 旁边。查找顺序：

`MAILBRIDGE_HOME` 环境变量 → `~/.mailbridge`（已配置时）→ 包内 `.mailbridge/`

所以带凭据的包在你自己的机器上仍会优先使用本机配置。

> ⚠️ 含凭据的包**就是机密文件**——绝不要发布或分享。万一泄露，应用专用密码和 Entra 注册都可以单独吊销。

## 安全

- 凭据**只**保存在运行 server 那台机器的 `~/.mailbridge/accounts.json`（权限 0600），除了你的邮件服务商之外不会发往任何地方。
- 密码永远不经过对话记录——向导以隐藏输入方式读取。
- 应用密码与 Entra 注册都可单独吊销：删掉即刻切断 mailbridge 的访问。
- 发送与彻底删除都被 `confirm=true` 显式参数拦住。

## 已知限制

- Graph（Outlook/Exchange）附件通过分块上传会话最大支持 150 MB；服务商的整封邮件大小上限仍然适用——IMAP 服务商同理（一般每封 20–50 MB）。
- IMAP 全文搜索在服务器端执行；部分服务商（尤其 QQ）对非 ASCII 关键词会静默返回空——请改用发件人或日期过滤。
- IMAP 上的 `mail_get_thread` 按规范化主题匹配而非真实会话头；Graph 账号使用真实会话 id。
- 多数大学会拦截未经 IT 批准的第三方应用（Entra 注册需管理员同意）——这是租户策略，mailbridge 无法绕过。

## 目录结构与测试

```
server/          MCP server（stdio JSON-RPC）、IMAP + Graph 后端、OAuth、i18n
skills/          Agent 工作流指南：邮箱配置 & 日常邮件处理
tests/           离线测试套件 —— 不联网、不需要凭据
```

```bash
python tests/test_mailbridge.py
```

## 致谢

由 **Sibo Huang** 出品。开发工作由 **Claude Opus 5**（初版实现，Cowork 中完成）与 **Claude Fable 5**（实机测试、缺陷修复、中英双语化、发布工程）协作完成。

## 许可证

[MIT](LICENSE) © 2026 Sibo Huang
