# Registering a Microsoft app for mailbridge

Needed once, for any Microsoft mailbox: personal Outlook.com/Hotmail/Live, or a
work/school Exchange Online account. One registration covers all of them. It is
free, needs no Azure subscription, and takes about five minutes.

Everything happens in a browser; follow the steps below as written.

## Steps

1. Open **https://entra.microsoft.com** and sign in with any Microsoft account.
   A personal account works — most get a "Default Directory" tenant
   automatically.

   > **If "App registrations" is missing, greyed out, or you get an access
   > error**: some personal accounts land in the shared **"Microsoft
   > Services"** tenant, where app registration is blocked. Fix: create your
   > own free tenant first — in the Entra admin center open the directory
   > switcher / **Manage tenants → Create**, make a new tenant (Workforce
   > type), switch into it, then continue below. Registering an app never
   > requires a paid Azure subscription; at most Microsoft asks you to
   > complete the free sign-up.

2. In the left sidebar: **Applications → App registrations → New registration**.

3. Fill in:
   - **Name**: `mailbridge` (any name is fine — it is only shown on the consent
     screen)
   - **Supported account types**: **"Accounts in any organizational directory
     (Any Microsoft Entra ID tenant - Multitenant) and personal Microsoft
     accounts (e.g. Skype, Xbox)"**

     This is the option that lets one registration serve both a personal
     Outlook.com mailbox and a university account. Picking a narrower option is
     the single most common setup mistake.
   - **Redirect URI**: leave blank. mailbridge uses the device code flow, which
     does not need one.

4. Click **Register**. On the Overview page that appears, copy the
   **Application (client) ID** — a GUID like
   `4a1b2c3d-5e6f-7890-abcd-ef1234567890`. This is what the setup wizard asks
   for.

5. Left sidebar of the new app: **Authentication**. Scroll to
   **Advanced settings** and set **"Allow public client flows"** to **Yes**.
   Click **Save**.

   Skipping this produces `AADSTS7000218` ("client_assertion or client_secret
   required") at sign-in.

6. Left sidebar: **API permissions → Add a permission → Microsoft Graph →
   Delegated permissions**. Tick:
   - `Mail.ReadWrite` — read mail, save drafts, move, flag, delete
   - `Mail.Send` — send mail
   - `User.Read` — read the signed-in user's basic profile
   - `offline_access` — issue a refresh token, so sign-in is a one-time thing

   Click **Add permissions**. No admin consent is needed here for a personal
   account; the user consents at sign-in.

## Then

Run the wizard, choose the Outlook or work/school option, and paste the client
ID when prompted. It will print a code and a URL — the user opens the URL, enters
the code, and signs in as the mailbox owner.

To authorise more mailboxes later, add each as its own account with the *same*
client ID and run `python3 setup.py auth NAME` for each.

## Tenant values

The `tenant` field in `accounts.json` controls which sign-in authority is used:

| Value | Use for |
|---|---|
| `consumers` | Personal Outlook.com / Hotmail / Live only |
| `organizations` | Work or school accounts only |
| `common` | Either — useful if unsure |
| a domain or GUID | Pin to one specific tenant, e.g. `contoso.edu` |

The wizard sets a sensible default. Pinning to a specific tenant domain
sometimes resolves `AADSTS50020` errors when a personal and work account share
the same email address.

## Errors and what they mean

| Code | Meaning | Fix |
|---|---|---|
| `AADSTS7000218` | Public client flows not enabled | Step 5 above |
| `AADSTS65001` | No consent recorded for this app in the tenant | The tenant admin must approve it |
| `AADSTS50020` | Account does not exist in the requested tenant | Correct the `tenant` field |
| `AADSTS900023` | Tenant identifier is not valid | Use `common`, `consumers` or `organizations` |
| `AADSTS50011` | Redirect URI mismatch | You are not on the device code flow — re-check step 3 |
| HTTP 403 from Graph | Permission missing or unconsented | Re-check step 6, then re-run `setup.py auth` |

## The university case

Universities almost always disable user consent for unverified publishers. The
user will hit a "Need admin approval" screen. Nothing in the app can bypass this
— it is the institution deliberately controlling which apps touch staff and
student mail.

The honest options are:

- Ask IT to grant admin consent for the app registration (they will want the
  client ID and the list of delegated scopes above). Some institutions have a
  formal request process for this; many will decline for a personally-registered
  app.
- Set up forwarding from the university mailbox to a mailbox the user does
  control, and read it there.
- Read that mailbox through its web interface when needed.

Present these plainly rather than encouraging repeated attempts.
