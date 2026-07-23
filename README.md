# P2P Desk Bot — Sell USDT for INR

Telegram bot for an admin-operated P2P desk. Users sell USDT (TRC20) and get paid
INR through **UPI · IMPS instant · CDM · Cheque transfer**, each with its own live
rate that admins set from chat. Admins work orders from copy-paste-ready cards with
one-tap **Done** — and can DM any user (text or screenshots) by simply replying to
the order card.

## User flow

1. `/start` → welcome with the user's name + Telegram ID → **USDT Sell / USDT Buy**
   buttons (Buy is a placeholder for now).
2. **Sell** → service buttons with live prices (e.g. `CDM — 1$/₹91`) → send amount
   in `$` → bank step:
   - first order: submit bank details once, saved to **My Bank Cards**
     (multiple banks supported, add/remove from the menu);
   - later orders: just **choose your bank**.
3. Order placed → bot shows the desk's TRC20 deposit address → user sends USDT and
   taps **I've sent the USDT** → "✅✅ Successfully submitted — funds to your bank
   within 15–30 minutes, often faster depending on the queue."
4. Admin taps **Done** on the order card → user gets "funds credited ✅✅" with the
   full details.
5. **Cancel** is available for 30 seconds after placing the order (configurable).
   On cancel the admin card updates instantly, the bot collects the user's TRC20
   address, and the card shows exactly how much USDT to refund where, with a
   **Refund sent** button.

A TRON auto-scan (instant deposit confirmation) is a planned next step — the
"I've sent" checkpoint is where it will plug in.

### Built for trust

- Every step carries a footer with the user's own Telegram name + ID and the
  live support contact(s).
- Orders get a searchable tag (`#ORD12`) shown identically to the user and on
  the admin card, so one Telegram search finds the whole trail.
- Admin cards include the user's name as a direct `tg://user` link, their
  @username link and chat ID — one tap to DM them outside the bot.
- Completion sends the user a full receipt: amount sold, locked rate, INR
  credited, bank, and timestamp (IST).

## Admin commands

| Command | What it does |
|---|---|
| `/admin` | list these commands |
| `/setrate CDM 91` | set a service's ₹/$ rate live (`0` hides the service) |
| `/rates` | show all rates + deposit address |
| `/setaddress T…` | set the TRC20 deposit address |
| `/setsupport @a @b` | set the support contact(s) shown to users everywhere |
| `/orders` | list open orders |
| `/order 12` (or `#ORD12`) | reshow an order card with its buttons |
| `/setstatus 12 completed` | force an order's status (repair tool) |
| `/ban` / `/unban <user_id>` | block/unblock a user |
| *reply to an order card* | DM that order's user through the bot (text/photo) |

## Setup

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in P2P_BOT_TOKEN and P2P_ADMIN_IDS at minimum
./.venv/bin/python -m bot.main
```

- Create the bot with [@BotFather](https://t.me/BotFather); get admin Telegram IDs
  from [@userinfobot](https://t.me/userinfobot).
- For a shared admin group, add the bot to the group and set `P2P_ADMIN_CHAT_ID`
  (group IDs are negative).
- First run: `/setaddress T…` and at least one `/setrate` — the sell menu stays
  closed until both exist.

All state lives in one SQLite file (`P2P_DB_PATH`). Rates and the deposit address
are chat-managed and survive restarts. Other knobs in [.env.example](.env.example):
per-order `$` min/max, cancel window seconds, payout ETA text, support handle.

## ⚠️ Compliance note

Buying crypto from the public for fiat generally makes you a Virtual Asset Service
Provider. In India that can mean FIU-IND registration, PMLA KYC/AML obligations and
VDA TDS rules apply to your desk — worth sizing with a professional. This software
only tracks orders and statuses; it moves no money and verifies nothing on-chain.
