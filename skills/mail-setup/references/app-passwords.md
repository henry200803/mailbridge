# Where each provider hides its app password

IMAP accounts need a provider-issued app password, not the account's login
password. Every provider below rejects the normal password outright.

Have the user generate these themselves and type them into the setup wizard,
which hides the input. Never ask for one in conversation.

## iCloud Mail

1. **https://account.apple.com** → sign in
2. **Sign-In and Security → App-Specific Passwords**
3. **Generate an app-specific password**, name it `mailbridge`
4. Copy the value — it looks like `abcd-efgh-ijkl-mnop`

Requires two-factor authentication on the Apple ID (it is on by default for
almost every account now). The password is shown once; regenerate if lost.
Revoking it from the same page instantly cuts mailbridge off, which makes it a
clean kill switch.

Servers: `imap.mail.me.com:993` (SSL), `smtp.mail.me.com:587` (STARTTLS).

## Gmail

1. Two-Step Verification must be on: **https://myaccount.google.com/security**
2. **https://myaccount.google.com/apppasswords**
3. Create one named `mailbridge`, copy the 16-character value

Google hides the App passwords page entirely until 2-Step Verification is
enabled. Workspace accounts may have it disabled by the domain admin.

Servers: `imap.gmail.com:993` (SSL), `smtp.gmail.com:587` (STARTTLS).

Note that Gmail's IMAP folder list includes `[Gmail]/All Mail`, which duplicates
every message. mailbridge excludes it from cross-folder searches on purpose.

## QQ 邮箱

1. Web mail → **设置 → 账户**
2. Scroll to **POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务**
3. Enable **IMAP/SMTP服务**, verify by SMS
4. Copy the **授权码** (authorization code)

Servers: `imap.qq.com:993` (SSL), `smtp.qq.com:465` (SSL).
Requires the IMAP `ID` command — mailbridge sends it automatically.

## 163 / 126 邮箱

1. Web mail → **设置 → POP3/SMTP/IMAP**
2. Enable **IMAP/SMTP服务**
3. Copy the **授权码**

Servers: `imap.163.com:993` / `imap.126.com:993` (SSL),
`smtp.163.com:465` / `smtp.126.com:465` (SSL).

These servers reject clients that do not send an IMAP `ID` command, failing with
"Unsafe Login. Please contact kefu@188.com". mailbridge sends it automatically;
if you configure one of these hosts manually, set `"needs_imap_id": true`.

## Yahoo Mail

**Account Info → Account Security → Generate app password**.
Servers: `imap.mail.yahoo.com:993` (SSL), `smtp.mail.yahoo.com:587` (STARTTLS).

## Fastmail

**Settings → Privacy & Security → Integrations → New app password**. Scope it to
"Mail (IMAP/SMTP)".
Servers: `imap.fastmail.com:993` (SSL), `smtp.fastmail.com:465` (SSL).

## Outlook.com, Hotmail, Live, Office 365, university mail

**No app password exists.** Microsoft retired Basic authentication for IMAP, POP
and SMTP on both consumer mailboxes and Exchange Online. Any guide offering an
Outlook app password for IMAP is out of date.

These accounts must use the Graph backend with OAuth — see
`azure-app-registration.md`.

## Anything else

Most providers publish their IMAP/SMTP hostnames under a "mail client setup" or
"IMAP settings" help page. Choose "Other (custom IMAP/SMTP host)" in the wizard.
Ports are almost always 993 for IMAP over SSL, and 465 (SSL) or 587 (STARTTLS)
for SMTP.
