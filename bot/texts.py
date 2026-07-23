"""Every user-facing message in one place, so wording is easy to tune."""

from .config import SERVICES, settings


def welcome(name: str, user_id: int) -> str:
    return (
        f"👋 Welcome, <b>{name}</b>!\n"
        f"🆔 Your ID: <code>{user_id}</code>\n\n"
        "🇮🇳 <b>The safest P2P trading in entire India.</b>\n\n"
        "We settle through <b>UPI · IMPS instant · CDM · Cheque transfer</b> — "
        "each service has its own rate, always live below.\n\n"
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
                 bank_label: str, address: str) -> str:
    return (
        f"📝 <b>Order #{order_id} placed!</b>\n\n"
        f"Sell: <b>{usd:g}$</b> via {service_label}\n"
        f"You receive: <b>₹{inr:,.2f}</b> → {bank_label}\n\n"
        f"Send <b>{usd:g} USDT (TRC20)</b> to:\n"
        f"<code>{address}</code>\n\n"
        "⚠️ TRC20 network only. Tap the button once you've sent it.\n"
        f"❌ You can cancel within {settings.cancel_window_sec} seconds of placing the order."
    )


def order_submitted(bank_details: str) -> str:
    return (
        "✅✅ <b>Successfully submitted!</b>\n\n"
        f"We will send your funds to:\n<code>{bank_details}</code>\n"
        f"within <b>{settings.eta_text}</b> — you can also receive it faster, "
        "it depends on the queue. 🟢\n\n"
        "Just relax, your funds will be credited. If we ever cross the timeline, "
        "your transaction fee is on us — included in your present order."
    )


def order_completed(order_id: int, inr: float, service_label: str, bank_details: str) -> str:
    return (
        f"✅✅ <b>Order #{order_id} completed — funds credited!</b> 🟢\n\n"
        f"Sent: <b>₹{inr:,.2f}</b> via {service_label}\n"
        f"To:\n<code>{bank_details}</code>\n\n"
        "Thanks for trading with the safest P2P desk in India. 🇮🇳"
    )


def order_cancelled(order_id: int) -> str:
    return (
        f"❌ <b>Order #{order_id} cancelled.</b>\n\n"
        "If you already sent the USDT, send your <b>TRC20 address</b> here now "
        "and we'll refund it. (Address starts with <code>T</code>.)"
    )


def refund_noted(usd: float, address: str) -> str:
    return (
        f"👌 Noted — <b>{usd:g} USDT</b> will be returned to:\n"
        f"<code>{address}</code>"
    )


def refund_sent(order_id: int, usd: float, address: str) -> str:
    return (
        f"💸 <b>Refund sent for order #{order_id}</b> — {usd:g} USDT to:\n"
        f"<code>{address}</code> ✅"
    )


CANCEL_WINDOW_OVER = (
    "The cancel window has passed and your order is already in processing. "
    f"Message {settings.support_handle} if you need help."
)

BUY_SOON = (
    "🛒 <b>USDT Buy is opening soon!</b>\n\n"
    f"Want to buy right now? Message {settings.support_handle} and we'll sort you out."
)

SUPPORT = (
    f"🆘 Any issue with an order? Message {settings.support_handle} "
    "and mention your order number."
)

DESK_CLOSED = "The desk is closed right now — please check back soon."
BANNED = "Your account is blocked. Contact support."
