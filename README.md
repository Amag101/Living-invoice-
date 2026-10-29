# Living Invoice

**The invoice negotiates for you.** A freelancer sends a real PayPal invoice with an AI agent attached. The client can ask it questions, ask for a payment plan or a discount, and pay. The agent agrees to whatever is within the freelancer's rules and sends anything else to the freelancer's phone. The rules are enforced in code (`policy.py`), so nothing a client types can talk the agent past them.

Built for the [PayPal AI Hackathon](https://paypalaihackathon.devpost.com/) on top of Invisible Software, a background-automation prototype. Everything below the PayPal section describes that base.

## PayPal integration (work in progress)

| Piece | File | Status |
|---|---|---|
| PayPal REST client: OAuth, Invoicing v2, Orders v2, Payouts v1, webhook verification, `PayPal-Request-Id` on every write | `paypal_api.py` | done |
| Fake PayPal sandbox for tests and offline demos | `fake_paypal.py` | done |
| Finished jobs become real PayPal invoices. Terms (discount, partial-payment minimum, due date) update in PayPal, each with an undo | `server.py` (`PayPalAccounting`) | done |
| Payments made on PayPal come back by verified webhook (`/webhooks/paypal`) with polling as a backup | `server.py` (`paypal_webhook`, `poll_paypal`) | done |
| Negotiation policy: ALLOW / ESCALATE / DENY | `policy.py` | done |
| Client-facing Living Invoice page at `/i/{token}`: invoice, chat with the negotiator, PayPal checkout | `app/invoice.html`, `server.py` (Living Invoice section) | done |
| Negotiator: Claude with four narrow tools (or keyword rules when the AI layer is off), bound by `policy.py` | `server.py` (`claude_reply`, `rule_reply`, `propose_offer`) | done |
| Owner escalations on the phone: approve, skip, or “counter #12 8%” / “$1,050” / “10 days” / “2 payments” | `server.py` (`counter`, `offer_settled`) | done |
| Subcontractor payouts: a paid-in-full invoice proposes the subcontractor's share with PayPal Payouts; first payout to anyone asks; undo cancels while unclaimed | `server.py` (`PayoutOnPaid`, `PayPalPayouts`) | done |
| Money ledger at `/ledger`: AG Grid of every invoice, request, decision, payment and payout, live totals, filters, undo | `app/ledger.html`, `server.py` (`ledger_state`) | done |
| Owner sets the agent's rules in plain English on the phone (“allow up to 4 payments”, “3% off for paying today”), confirms a before/after preview | `server.py` (`propose_mandate`) | done |
| Send an invoice directly from the New invoice tab (new or existing client, optional subcontractor share) | `app/index.html`, `server.py` (`new_invoice`) | done |
| The agent earns wider rules: three approvals of the same kind of request lead to an offer to handle those itself; an undo narrows the rule back | `server.py` (`maybe_grow`, `maybe_shrink`) | done |
| Owner Assistant on the PayPal Agent Toolkit | — | planned |

### Run against the PayPal sandbox

Copy `.env.example` to `.env`, fill in your sandbox app's client id and secret, then export the variables and start the server:

```bash
set -a; source .env; set +a; python3 server.py
```

Without credentials the app falls back to its offline accounting, so the demo still runs.

### Run with the fake PayPal sandbox (no PayPal account needed)

In one terminal:

```bash
python3 fake_paypal.py --webhook http://localhost:8790/webhooks/paypal
```

In another:

```bash
PAYPAL_API_BASE=http://localhost:8791 PAYPAL_CLIENT_ID=fake PAYPAL_CLIENT_SECRET=fake PAYPAL_WEBHOOK_ID=WH-FAKE-1 python3 server.py
```

Mark a job done and approve the invoice on the phone, then open **Client view** in the Accounting tab. In fake mode the pay button stands in for PayPal's approval popup; with real sandbox credentials the page shows PayPal's own buttons.

A payer paying on PayPal's own page is simulated with `curl -X POST localhost:8791/__fake/invoices/<PayPal invoice id>/pay -d '{}'`. The fake then sends a signed `INVOICING.INVOICE.PAID` webhook to the app.

---

# Invisible Software (base prototype)

Background automation that works inside the tools a business already uses. The owner never opens a dashboard. Approvals, notices and the weekly summary arrive as chat messages, and every action is logged with a way to undo it.

This prototype builds Phase 0 of the product brief: the invoice and payment-chasing loop, with the trust model, hard limits, undo and an audit log. Gmail and Google Calendar connect for real over OAuth. Accounting and WhatsApp are still simulated.

## Run it

You need Python 3.9 or newer. The demo runs with no installs. Connecting Google needs one package for encrypted token storage:

```bash
pip3 install cryptography
```

Then start the server:

```bash
python3 server.py
```

Then open http://localhost:8790. Data lives in `invisible.db` next to `server.py`. Use **Reset demo** in the page, or delete that file, to start over.

## What you see

The left side holds the demo business's own tools: a calendar, accounting, and a log of what was sent to clients. A simulated clock lets you move time forward. The right side is the owner's phone, the only screen the owner would ever see. `/audit` is the audit log, the one web page the product keeps.

## Things to try

| Do this | What happens |
|---|---|
| Mark "Brand workshop" done | Invisible asks on the phone whether to invoice Acme Dental $1,200, with the reason and a preview of the email |
| Tap **Approve** | An invoice is created in accounting and emailed to the client |
| Reply **undo** | The invoice is voided and a correction email goes out |
| Click **+1 day** | Overdue reminders are proposed: friendly for INV-1001, firm for INV-1002. An appointment reminder to Nnamdi Eze asks first because it would be the first message to him |
| Record payment on INV-1001 while its reminder is pending | The reminder is withdrawn automatically |
| Mark "Rebrand package handover" done | It always asks, because $7,500 is above the $5,000 limit |
| Approve three invoices in a row | That automation switches to automatic and tells you after each action |
| Undo an automatic action | That automation drops back to asking first |
| Advance to Monday | The weekly summary arrives with the admin time saved |
| Type "remind clients 48 hours before appointments by email" | Invisible restates the rule in plain words and asks before saving it |
| Type "pause summary", "auto invoices" or "ask first for overdue reminders" | Changes that automation's trust level or pauses it |

## Connect Gmail and Google Calendar

One-time setup in Google Cloud Console:

1. Create a project, then enable the **Gmail API** and the **Google Calendar API**.
2. Configure the OAuth consent screen as **External**, leave it in **Testing**, and add your Google address as a test user.
3. Create an **OAuth client ID** of type **Web application**. Add this authorized redirect URI: `http://localhost:8790/oauth/google/callback`
4. Download the client JSON and save it as `google_client.json` next to `server.py`. You can also set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` instead.
5. Restart the server, open `/audit`, and click **Connect Google**.

Once connected:

- **Real time.** The simulated clock switches to real time. A background loop syncs the calendar every 60 seconds; set `INVISIBLE_SYNC_SECONDS` to change that. **Sync Google now** forces a sync.
- **Billing a job.** Add `#done` to an event's title in Google Calendar, or click **Mark done** in the demo page, which writes the tag to Google. The client is the event's first guest who isn't you. The amount comes from the description, for example "Amount: 1200" or "$1,200".
- **Test mode is on by default.** Every email meant for a client is sent to your own address, with the intended recipient in the subject. Turn on **Send emails to clients for real** on the audit page when you're ready.
- **First sync is a baseline.** Events already tagged `#done` when you connect don't trigger invoices. Only changes after connecting do.
- **Reminders by email.** Google guests have no phone number, so WhatsApp reminders fall back to email for them.
- **Disconnect** revokes access at Google, deletes the stored tokens, and returns to the demo calendar.

### Permissions requested

| Scope | Why |
|---|---|
| `gmail.send` | Send invoices and reminders. It can't read your inbox. |
| `calendar.events` | Read events and add `#done` when you mark a job done. |
| `openid`, `email` | Know which account is connected. |

If Google grants fewer scopes than requested, the connection is refused and the token revoked.

### How tokens are protected

- **OAuth flow.** Authorization code flow with PKCE (S256) and a single-use `state` value that expires after 10 minutes.
- **Envelope encryption.** A master key wraps a per-business data key, and the data key encrypts the tokens with Fernet (AES plus HMAC). The database holds only the wrapped key and ciphertext.
- **Master key location.** The master key is read from `INVISIBLE_MASTER_KEY`. Otherwise it lives in `~/.invisible/master.key`, created with owner-only permissions. The server refuses to start the vault if that file is readable by other users. In production this would be a cloud KMS key.
- **Purpose binding.** Each ciphertext is bound to its purpose, so a blob copied into another row won't decrypt.
- **Never exposed.** Tokens never reach the browser or the logs. The ID token is discarded after sign-in.
- **Expiry and revocation.** Expired access tokens are refreshed automatically, and a 401 triggers one refresh and retry. If the refresh token is revoked, the owner gets one chat message asking them to reconnect.

## How it works

```
connectors -> event queue -> rules engine -> policy and trust gate -> executor -> audit log
                                                      |
                                               owner channel (chat)
```

- **Connectors** are the only code that touches a tool. Each one has `execute` and `inverse` methods, so every write is stored together with the write that reverses it. Gmail and Google Calendar are real once connected (`google_api.py`). Sandbox connectors stand in for WhatsApp Business and QuickBooks or Xero.
- **Event queue.** Every inbound event is stored first and deduplicated by key.
- **Rules engine.** There are four catalogue automations. Each turns events into proposed actions with a one-line reason, and re-checks its preconditions just before acting.
- **Policy and trust.** Each automation has one of four levels: watch only, ask first, automatic with notice, or automatic and silent. Three approvals in a row promote an automation to automatic, and an undo demotes it. Two hard limits always require approval whatever the level: invoices above the ceiling, and the first message to a new client.
- **Audit log.** The log is append-only. Undo replays the stored inverse writes. A message that was already sent can't be unsent, so its inverse is a correction message.

## Optional AI layer

Without it, chat commands and rule requests are parsed by plain rules. With it, Claude parses free-form requests and redrafts reminder emails. A redraft is only used if it still contains the invoice number and amount.

```bash
pip3 install anthropic
```

Credentials come from `ANTHROPIC_API_KEY` or an `ant auth login` profile. The model defaults to `claude-opus-5-5`, and you can change it with `INVISIBLE_MODEL`. Requests use server-side refusal fallbacks. Set `INVISIBLE_LLM=off` to force the AI layer off.

## Tests

```bash
python3 tests/test_flow.py
```

```bash
python3 tests/test_google.py
```

```bash
python3 tests/test_policy.py
```

```bash
python3 tests/test_living.py
```

```bash
python3 tests/test_new_invoice.py
```

```bash
python3 tests/test_growth.py
```

```bash
python3 tests/test_payouts.py
```

```bash
python3 tests/test_rules.py
```

```bash
python3 tests/test_paypal.py
```

The first suite covers 14 engine flows. The second runs 14 Google scenarios against a local fake Google server: the OAuth flow, refused and reused `state`, missing scopes, encryption at rest, token refresh, revoked access, calendar sync, `#done` invoicing through Gmail, test mode and live mode, cancellations and disconnect. Neither suite touches the network.

## Not built yet

- Real connectors for the WhatsApp Cloud API, QuickBooks and Xero.
- Google push notifications. Calendar changes arrive by polling, because push needs a public HTTPS address.
- Google's app verification. The `gmail.send` and `calendar.events` scopes are sensitive, so anyone outside your test users needs a verified app.
- Running WhatsApp as the real owner channel.
- Editing a draft before approving it.
- Accounts for several businesses, and staff roles.
