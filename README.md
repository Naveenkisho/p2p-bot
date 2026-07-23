# P2P Desk Bot — Sell USDT for INR

Telegram bot for an admin-operated P2P desk. Users sell USDT (TRC20) and get paid
INR through **UPI · IMPS instant · CDM · Cheque transfer**, each with its own live
rate that admins set from chat. Admins work orders from copy-paste-ready cards with
one-tap **Done** — and can DM any user (text or screenshots) by simply replying to
the order card.

## User flow (deposit-first, auto-detected)

1. `/start` → welcome with the user's name + Telegram ID → **USDT Sell / USDT Buy**
   buttons (Buy is a placeholder for now).
2. **Sell** → service buttons with live prices (e.g. `CDM — 1$/₹91`) → amount in
   `$` → the order is created instantly and the bot shows the desk's TRC20
   deposit address: *"send exactly 100 USDT"*.
3. **TRON auto-scan**: the bot polls TronGrid (every 10 s by default) for USDT
   transfers into the deposit address. The moment the user's transfer confirms,
   it is matched to the oldest awaiting order with that amount and the user gets
   *"✅✅ We received your 100 USDT!"* with the tx hash — typically seconds after
   confirmation. No screenshots, no "I've sent" honor system.
4. Then the bank step: pick a saved bank from **My Bank Cards** or add one →
   *"successfully submitted — funds within 15–30 min"* with a live queue position.
5. The admin card (bank details, tx hash, copy-paste blocks) gets a **Done**
   button → user receives a full receipt; the proof channel gets its card.
6. Safety nets: **Cancel** while awaiting deposit; unmatched deposits alert the
   admins with the tx hash; awaiting orders **expire** after 60 min (configurable);
   `/received <id>` confirms a deposit manually if TronGrid is ever down; the
   DB-driven refund path handles any cancelled-after-deposit case.

### Built for trust

- Every step carries a footer with the user's own Telegram name + ID and the
  live support contact(s).
- Orders get a searchable tag (`#ORD12`) shown identically to the user and on
  the admin card, so one Telegram search finds the whole trail.
- Admin cards include the user's name as a direct `tg://user` link, their
  @username link and chat ID — one tap to DM them outside the bot.
- Completion sends the user a full receipt: amount sold, locked rate, INR
  credited, bank, and timestamp (IST).
- Orders show a live queue position ("You're #3 — moves up on every payout").
- 📋 My Orders gives users their last 10 orders with statuses.
- A bare screenshot replied to an order card is delivered to the user
  auto-captioned as "🧾 Payment proof — order #ORD12".
- Optional public proof channel: completed orders post an anonymized card
  ("✅ #ORD12 — 100$ → ₹9,100 via IMPS — paid in 14 min").

## Admin commands

| Command | What it does |
|---|---|
| `/admin` | list these commands |
| `/setrate CDM 91` | set a service's ₹/$ rate live (`0` hides the service) |
| `/rates` | show all rates + deposit address |
| `/setaddress T…` | set the TRC20 deposit address |
| `/setsupport @a @b` | set the support contact(s) shown to users everywhere |
| `/setchannel @channel` | public proof channel — every completed order posts an anonymized proof card (`off` disables; bot must be channel admin) |
| `/panel` or `/orders` | live tabbed order panel — ⏳ Active / ↩️ Refunds / ✅ Done, every tap refreshes |
| `/order 12` (or `#ORD12`) | reshow an order card with its buttons |
| `/setstatus 12 completed` | force an order's status (repair tool) |
| `/setrefund 12 T…` | record a refund address on the user's behalf |
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
