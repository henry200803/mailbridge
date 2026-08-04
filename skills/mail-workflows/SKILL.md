---
name: mail-workflows
description: Handles email tasks across the user's connected mailboxes — searching for a specific message, triaging or summarising the inbox, drafting and sending replies, filing and cleaning up mail, and bulk-exporting messages or attachments. Use whenever the user asks about their email, says "check my inbox", "find that email from", "what needs a reply", "draft a response to", "unsubscribe me from", "export all receipts", or refers to mail in iCloud, Outlook, Gmail or a university mailbox.
---

# Working with the user's mail

mailbridge exposes real mailbox access. Treat it the way a careful assistant with
someone's inbox open would: read freely, change things deliberately, and never
send without an explicit go-ahead.

## Orient first

Call `mail_list_accounts` at the start of a mail task unless the account is
already established in this conversation. Everything else depends on knowing
which mailboxes exist and what they are called.

Omitting `accounts` on `mail_search`, `mail_digest` and `mail_export` searches
every configured mailbox at once. That is usually what the user means by "my
email". Name specific accounts only when they do.

## Searching

`mail_search` returns summaries — sender, subject, date, snippet, flags, id — and
never bodies. Read bodies with `mail_get_message`, and only for messages that
actually matter. Pulling twenty full bodies to answer one question wastes the
user's context and slows everything down.

Compose filters instead of running several searches:

```
mail_search(sender="qantas", since="2026-06-01", has_attachment=true)
mail_search(query="lease renewal", folder="archive", limit=50)
mail_search(unread_only=true, since="2026-07-28")
```

Practical notes:

- `query` is a full-text search. Both backends support it, but IMAP full-text
  search is slow on large mailboxes — add `since` or `folder` to narrow it.
- `sender` matches the address or display name; a bare domain like `qantas`
  works well.
- Dates take `YYYY-MM-DD` or full ISO-8601. `before` is exclusive.
- `limit` is per account, so a limit of 25 across four mailboxes can return 100.
- Message ids are backend-specific and can go stale when mail moves. If a tool
  reports an id as missing, search again rather than guessing.

When the user asks about a conversation rather than one message, use
`mail_get_thread` — on Outlook and Exchange it follows the real conversation id.

## Triage and daily review

Use `mail_digest` for "what needs my attention", "catch me up", or a morning
review. One call covers every account and returns unread counts plus recent
unread summaries. Do not reconstruct this from a pile of searches.

Then add judgement, which is the part the tool cannot do. Sort what came back
into things needing a reply, things that are informational, and noise. Name
senders and subjects concretely. Say how old something is when that changes its
urgency. Skip the ceremony of restating counts the user can see.

## Drafting and sending

Default to `mail_draft`. It saves to the Drafts folder, threads correctly when
given `reply_to_message_id`, and leaves the user in control of the final send.
For a reply, pass `reply_to_message_id` and omit `to` — the recipient is
inferred from the original.

`mail_send` sends immediately and requires `confirm=true`. Before setting that
flag: show the user the exact recipients, subject and body, and get an explicit
yes in conversation. A previous general instruction to "handle my email" is not
approval for a specific message. If there is any doubt at all, draft instead and
say so.

Write in the user's voice, not a generic assistant register. If a
`my-writing-style` profile exists, use it. Match the register of the thread being
replied to — a one-line reply to a one-line question, not three paragraphs.

## Filing, flagging and deleting

`mail_mark` handles read/unread and flagged/unflagged in bulk. `mail_move` files
messages; call `mail_list_folders` first if you are unsure of the exact
destination name, or pass a role like `archive` or `trash`.

`mail_delete` moves to Trash by default, which is recoverable. `permanent=true`
is irreversible and additionally requires `confirm=true`; list what will be
destroyed and get approval before using it. Bulk cleanup — "delete all the
newsletters" — should almost always go to Trash, and should be preceded by
showing the user the matched set.

## Bulk work

`mail_export` writes a full result set to CSV or JSON on disk rather than into
the conversation. Reach for it whenever the answer involves more than roughly
thirty messages: receipts for a tax year, everything from one sender, an
inventory of what is taking up space. Analyse the file afterwards with code.

`mail_download_attachment` takes the attachment `id` or `filename` from a prior
`mail_get_message` call. For many attachments, export first to identify the
messages, then download in a loop.

## Boundaries worth keeping

Mail is private and often contains other people's information. Read what the
task requires and no more; do not go browsing adjacent messages because they
looked interesting. When summarising, do not surface unrelated sensitive
material the user did not ask about.

Never write a credential, verification code, or password-reset link into the
conversation, even when it appears in a message being summarised. Say that a code
arrived and let the user open it.
