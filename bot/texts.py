"""Every user-facing message in one place, so wording is easy to tune.

User-controlled values (names, bank details, labels) are HTML-escaped HERE,
so callers pass them raw.
"""

import html

from .config import SERVICES, settings


def tag(order_id: int) -> str:
    """Search-friendly order tag — same on the user and admin side, so one
    Telegram search for #ORD12 finds the whole trail."""
    return f"#ORD{order_id}"


def trust_footer(name: str | None, user_id: int, support: str) -> str:
    """Appended to every flow step: who this chat belongs to + live support."""
    return (
        "\n\n———\n"
        f"👤 <b>{html.escape(name or 'friend')}</b> · 🆔 <code>{user_id}</code>\n"
        f"🆘 Support: {html.escape(support)}\n"
        "🛡 Every order is handled personally by our admins."
    )


def welcome(name: str | None, user_id: int, support: str) -> str:
    return (
        f"👋 Welcome, <b>{html.escape(name or 'friend')}</b>!\n"
        f"🆔 Your ID: <code>{user_id}</code>\n\n"
        "🇮🇳 <b>The safest P2P trading in entire India.</b>\n\n"
        "We settle through <b>UPI · IMPS instant · CDM · Cheque transfer</b> — "
        "each service has its own rate, always live below.\n\n"
        f"🆘 Support: {html.escape(support)}\n\n"
        "What would you like to do?"
    )


def services_header(rates: dict[str, float]) -> str:
    lines = ["💵 <b>Sell USDT — choose your payout service</b>", ""]
    for key, rate in rates.items():
        lines.append(f"• {SERVICES[key]} — <b>1$ / ₹{rate:g}</b>")
    lines.append("")
    lines.append("Rates are live and locked for your order once you choose.")
    return "\n".join(lines)


def ask_amount(service_label: str, rate: float) -> str:
    return (
        f"You picked <b>{service_label}</b> at <b>1$ / ₹{rate:g}</b>.\n\n"
        f"How much do you want to sell, in <b>$</b>? "
        f"({settings.min_usd:g}–{settings.max_usd:g})\n"
        "Just send the number, e.g. <code>100</code>."
    )


def quote_block(usd: float, inr: float, service_label: str, rate: float) -> str:
    return (
        f"💰 You send: <b>{usd:g}$ USDT (TRC20)</b>\n"
        f"💵 You receive: <b>₹{inr:,.2f}</b> via {service_label}\n"
        f"💱 Rate locked: <b>1$ / ₹{rate:g}</b>"
    )


ASK_BANK_FIRST = (
    "🏦 First time here — send your <b>bank details in one message</b>, one item per line:\n\n"
    "<code>Bank name\n"
    "Account holder name\n"
    "Account number\n"
    "IFSC</code>\n\n"
    "It's saved to <b>My Bank Cards</b> so next time you just pick it."
)

ASK_BANK_NEW = (
    "🏦 Send the <b>new bank's details in one message</b>, one item per line:\n\n"
    "<code>Bank name\nAccount holder name\nAccount number\nIFSC</code>"
)

CHOOSE_BANK = "🏦 Final step — <b>choose your bank</b> for the payout:"


def order_placed(order_id: int, usd: float, inr: float, service_label: str,
                 bank_label: str, address: str, rate: float,
                 rate_note: str = "") -> str:
    return (
        f"📝 <b>Order {tag(order_id)} placed!</b>\n"
        f"🧾 Order ID: <code>{tag(order_id)}</code> — quote it to support anytime.\n\n"
        f"{rate_note}"
        f"Sell: <b>{usd:g}$</b> via {service_label} at 1$/₹{rate:g}\n"
        f"You receive: <b>₹{inr:,.2f}</b> → {html.escape(bank_label)}\n\n"
        f"Send <b>{usd:g} USDT (TRC20)</b> to:\n"
        f"<code>{address}</code>\n\n"
        "⚠️ TRC20 network only. Tap the button once you've sent it.\n"
        f"❌ You can cancel within {settings.cancel_window_sec} seconds of placing the order."
    )


def order_submitted(order_id: int, bank_details: str) -> str:
    return (
        f"✅✅ <b>Order {tag(order_id)} successfully submitted!</b>\n\n"
        f"We will send your funds to:\n<code>{html.escape(bank_details)}</code>\n"
        f"within <b>{settings.eta_text}</b> — you can also receive it faster, "
        "it depends on the queue. 🟢\n\n"
        "Just relax, your funds will be credited. If we ever cross the timeline, "
        "your transaction fee is on us — included in your present order."
    )


def order_completed(order_id: int, usd: float, rate: float, inr: float,
                    service_label: str, bank_details: str, when: str) -> str:
    return (
        f"✅✅ <b>Order {tag(order_id)} completed — funds credited!</b> 🟢\n\n"
        "🧾 <b>Receipt</b>\n"
        f"• Order ID: <code>{tag(order_id)}</code>\n"
        f"• Sold: <b>{usd:g}$ USDT</b> at 1$/₹{rate:g}\n"
        f"• Credited: <b>₹{inr:,.2f}</b> via {service_label}\n"
        f"• To:\n<code>{html.escape(bank_details)}</code>\n"
        f"• Completed: {when}\n\n"
        "Save this message as your receipt. "
        "Thanks for trading with the safest P2P desk in India. 🇮🇳"
    )


def order_cancelled(order_id: int) -> str:
    return (
        f"❌ <b>Order {tag(order_id)} cancelled.</b>\n\n"
        "If you already sent the USDT, send your <b>TRC20 address</b> here now "
        "and we'll refund it. (Address starts with <code>T</code>.)"
    )


def refund_noted(order_id: int, usd: float, address: str) -> str:
    return (
        f"👌 Noted for order {tag(order_id)} — <b>{usd:g} USDT</b> will be returned to:\n"
        f"<code>{address}</code>"
    )


def refund_sent(order_id: int, usd: float, address: str) -> str:
    return (
        f"💸 <b>Refund sent for order {tag(order_id)}</b> — {usd:g} USDT to:\n"
        f"<code>{address}</code> ✅"
    )


def cancel_window_over(support: str) -> str:
    return ("The cancel window has passed and your order is already in processing. "
            f"Message {html.escape(support)} if you need help.")


def buy_soon(support: str) -> str:
    return ("🛒 <b>USDT Buy is opening soon!</b>\n\n"
            f"Want to buy right now? Message {html.escape(support)} and we'll sort you out.")


def support_text(support: str) -> str:
    return (f"🆘 Any issue with an order? Message {html.escape(support)} "
            "and mention your order ID (like <code>#ORD12</code>).")


DESK_CLOSED = "The desk is closed right now — please check back soon."
BANNED = "Your account is blocked. Contact support."
