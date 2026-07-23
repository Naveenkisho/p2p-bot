"""Every user-facing message in one place, in English ("en") and Roman-Hindi
Hinglish ("hi"). Functions take `lang` as the last argument and fall back to
English. User-controlled values (names, bank details, labels) are HTML-escaped
HERE, so callers pass them raw. Admin-side texts stay English by design.
"""

import html

from .config import SERVICES, settings

STATUS_EMOJI = {
    "awaiting_deposit": "⏳",
    "deposit_received": "📥",
    "pending_payout": "💰",
    "completed": "✅",
    "cancelled": "❌",
    "expired": "⌛",
    "refund_requested": "↩️",
    "refunded": "💸",
}


def tag(order_id: int) -> str:
    """Search-friendly order tag — same on the user and admin side, so one
    Telegram search for #ORD12 finds the whole trail."""
    return f"#ORD{order_id}"


def trust_footer(name: str | None, user_id: int, support: str, lang: str = "en") -> str:
    who = html.escape(name or ("dost" if lang == "hi" else "friend"))
    if lang == "hi":
        return (
            "\n\n———\n"
            f"👤 <b>{who}</b> · 🆔 <code>{user_id}</code>\n"
            f"🆘 Support: {html.escape(support)}\n"
            "🛡 Har order hamare admins khud personally handle karte hain."
        )
    return (
        "\n\n———\n"
        f"👤 <b>{who}</b> · 🆔 <code>{user_id}</code>\n"
        f"🆘 Support: {html.escape(support)}\n"
        "🛡 Every order is handled personally by our admins."
    )


def welcome(name: str | None, user_id: int, support: str, lang: str = "en") -> str:
    who = html.escape(name or ("dost" if lang == "hi" else "friend"))
    if lang == "hi":
        return (
            f"👋 Welcome, <b>{who}</b>!\n"
            f"🆔 Aapki ID: <code>{user_id}</code>\n\n"
            "🇮🇳 <b>India ki sabse safe P2P trading.</b>\n\n"
            "Hum settle karte hain <b>UPI · IMPS instant · CDM · Cheque transfer</b> "
            "se — har service ka apna live rate hai.\n\n"
            f"🆘 Support: {html.escape(support)}\n\n"
            "Kya karna chahenge?"
        )
    return (
        f"👋 Welcome, <b>{who}</b>!\n"
        f"🆔 Your ID: <code>{user_id}</code>\n\n"
        "🇮🇳 <b>The safest P2P trading in entire India.</b>\n\n"
        "We settle through <b>UPI · IMPS instant · CDM · Cheque transfer</b> — "
        "each service has its own rate, always live below.\n\n"
        f"🆘 Support: {html.escape(support)}\n\n"
        "What would you like to do?"
    )


def services_header(rates: dict[str, float], lang: str = "en") -> str:
    if lang == "hi":
        lines = ["💵 <b>USDT Sell — payout service chunein</b>", ""]
    else:
        lines = ["💵 <b>Sell USDT — choose your payout service</b>", ""]
    for key, rate in rates.items():
        lines.append(f"• {SERVICES[key]} — <b>1$ / ₹{rate:g}</b>")
    lines.append("")
    if lang == "hi":
        lines.append("Rates live hain — service chunte hi aapke order ke liye lock ho jaate hain.")
    else:
        lines.append("Rates are live and locked for your order once you choose.")
    return "\n".join(lines)


def ask_amount(service_label: str, rate: float, lang: str = "en") -> str:
    if lang == "hi":
        return (
            f"Aapne chuna <b>{service_label}</b> — <b>1$ / ₹{rate:g}</b>.\n\n"
            f"Kitne <b>$</b> bechne hain? ({settings.min_usd:g}–{settings.max_usd:g})\n"
            "Bas number bhejein, jaise <code>100</code>."
        )
    return (
        f"You picked <b>{service_label}</b> at <b>1$ / ₹{rate:g}</b>.\n\n"
        f"How much do you want to sell, in <b>$</b>? "
        f"({settings.min_usd:g}–{settings.max_usd:g})\n"
        "Just send the number, e.g. <code>100</code>."
    )


def rate_updated_note(rate: float, lang: str = "en") -> str:
    if lang == "hi":
        return f"📈 Aapke quote ke baad rate update hua: <b>1$ / ₹{rate:g}</b>\n\n"
    return f"📈 Rate updated since your quote: <b>1$ / ₹{rate:g}</b>\n\n"


def deposit_request(order_id: int, usd: float, inr: float, service_label: str,
                    address: str, rate: float, rate_note: str = "",
                    lang: str = "en") -> str:
    if lang == "hi":
        return (
            f"📝 <b>Order {tag(order_id)} ban gaya!</b>\n"
            f"🧾 Order ID: <code>{tag(order_id)}</code> — support ke liye yahi ID batayein.\n\n"
            f"{rate_note}"
            f"Sell: <b>{usd:g}$</b> via {service_label} @ 1$/₹{rate:g}\n"
            f"Aapko milenge: <b>₹{inr:,.2f}</b>\n\n"
            f"Bhejein <b>exactly {usd:g} USDT (TRC20)</b> is address par:\n"
            f"<code>{address}</code>\n\n"
            "⚡ Hamara system blockchain khud watch karta hai — deposit confirm hote "
            "hi seconds me yahan message aayega, phir bas apna bank chunna hai.\n\n"
            "⚠️ Sirf TRC20 network, exact amount.\n"
            f"⌛ Ye order {settings.deposit_ttl_min} minute tak open rahega."
        )
    return (
        f"📝 <b>Order {tag(order_id)} created!</b>\n"
        f"🧾 Order ID: <code>{tag(order_id)}</code> — quote it to support anytime.\n\n"
        f"{rate_note}"
        f"Sell: <b>{usd:g}$</b> via {service_label} at 1$/₹{rate:g}\n"
        f"You receive: <b>₹{inr:,.2f}</b>\n\n"
        f"Send <b>exactly {usd:g} USDT (TRC20)</b> to:\n"
        f"<code>{address}</code>\n\n"
        "⚡ Our system watches the blockchain — your deposit is auto-detected, "
        "usually within seconds of confirmation. You'll get an instant message "
        "here, then just choose your bank for the payout.\n\n"
        "⚠️ TRC20 network only, exact amount only.\n"
        f"⌛ This order stays open for {settings.deposit_ttl_min} minutes."
    )


def deposit_received(order_id: int, usd: float, inr: float, txid: str,
                     lang: str = "en") -> str:
    tx_note = f"🔗 TX: <code>{html.escape(txid)}</code>\n" \
        if txid and txid != "manual" else ""
    if lang == "hi":
        return (
            f"✅✅ <b>Aapke {usd:g} USDT mil gaye!</b> — Order {tag(order_id)} 🟢\n"
            f"{tx_note}\n"
            f"Aapka payout: <b>₹{inr:,.2f}</b>\n\n"
            "🏦 Last step — niche apna <b>bank chunein</b> (ya naya add karein), "
            "funds turant process honge."
        )
    return (
        f"✅✅ <b>We received your {usd:g} USDT!</b> — Order {tag(order_id)} 🟢\n"
        f"{tx_note}\n"
        f"Your payout: <b>₹{inr:,.2f}</b>\n\n"
        "🏦 Final step — <b>choose your bank</b> below (or add one) and your "
        "funds are on the way."
    )


def queue_note(position: int, lang: str = "en") -> str:
    if lang == "hi":
        if position <= 1:
            return "🚀 Aap <b>queue me pehle</b> ho — payout fatafat milega!\n\n"
        return f"📊 Queue position: <b>#{position}</b> — har payout ke saath upar aayenge.\n\n"
    if position <= 1:
        return "🚀 You're <b>first in the queue</b> — payout comes fast!\n\n"
    return f"📊 Queue position: <b>#{position}</b> — moves up on every payout.\n\n"


def order_submitted(order_id: int, bank_details: str, q_note: str = "",
                    lang: str = "en") -> str:
    if lang == "hi":
        return (
            f"✅✅ <b>Order {tag(order_id)} successfully submit ho gaya!</b>\n\n"
            f"{q_note}"
            f"Hum aapke funds bhejenge:\n<code>{html.escape(bank_details)}</code>\n"
            f"<b>{settings.eta_text}</b> ke andar — queue ke hisaab se aur jaldi "
            "bhi mil sakta hai. 🟢\n\n"
            "Bas relax karein, funds credit ho jayenge. Agar hum timeline cross "
            "karein to transaction fee hamari — aapke isi order me included."
        )
    return (
        f"✅✅ <b>Order {tag(order_id)} successfully submitted!</b>\n\n"
        f"{q_note}"
        f"We will send your funds to:\n<code>{html.escape(bank_details)}</code>\n"
        f"within <b>{settings.eta_text}</b> — you can also receive it faster, "
        "it depends on the queue. 🟢\n\n"
        "Just relax, your funds will be credited. If we ever cross the timeline, "
        "your transaction fee is on us — included in your present order."
    )


def order_completed(order_id: int, usd: float, rate: float, inr: float,
                    service_label: str, bank_details: str, when: str,
                    lang: str = "en") -> str:
    if lang == "hi":
        return (
            f"✅✅ <b>Order {tag(order_id)} complete — funds credit ho gaye!</b> 🟢\n\n"
            "🧾 <b>Receipt</b>\n"
            f"• Order ID: <code>{tag(order_id)}</code>\n"
            f"• Becha: <b>{usd:g}$ USDT</b> @ 1$/₹{rate:g}\n"
            f"• Credit hua: <b>₹{inr:,.2f}</b> via {service_label}\n"
            f"• Bank:\n<code>{html.escape(bank_details)}</code>\n"
            f"• Time: {when}\n\n"
            "Ye message apni receipt ke roop me save kar lein. "
            "India ke sabse safe P2P desk ke saath trade karne ka shukriya! 🇮🇳"
        )
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


def order_cancelled(order_id: int, lang: str = "en") -> str:
    if lang == "hi":
        return (
            f"❌ <b>Order {tag(order_id)} cancel ho gaya.</b>\n\n"
            "Kuch nahi bheja? Done — kabhi bhi naya order shuru karein.\n"
            "USDT bhej chuke hain? Apna <b>TRC20 address</b> (jo <code>T</code> se "
            "shuru hota hai) yahan bhejein, refund ho jayega."
        )
    return (
        f"❌ <b>Order {tag(order_id)} cancelled.</b>\n\n"
        "Didn't send anything? You're done — start a fresh order anytime.\n"
        "Already sent the USDT? Send your <b>TRC20 address</b> here now "
        "(starts with <code>T</code>) and we'll sort the refund."
    )


def order_expired(order_id: int, lang: str = "en") -> str:
    if lang == "hi":
        return (
            f"⌛ <b>Order {tag(order_id)} expire ho gaya</b> — time par deposit nahi mila.\n\n"
            "Kuch bheja nahi tha to koi baat nahi — naya order shuru karein. "
            "USDT bheja tha? Support ko order ID ke saath message karein."
        )
    return (
        f"⌛ <b>Order {tag(order_id)} expired</b> — we didn't see a deposit in time.\n\n"
        "Nothing to worry about if you didn't send anything — just start a fresh "
        "order. If you DID send USDT, message support with your order ID."
    )


def refund_noted(order_id: int, usd: float, address: str, lang: str = "en") -> str:
    if lang == "hi":
        return (
            f"👌 Note ho gaya order {tag(order_id)} ke liye — <b>{usd:g} USDT</b> "
            f"wapas aayenge:\n<code>{address}</code>"
        )
    return (
        f"👌 Noted for order {tag(order_id)} — <b>{usd:g} USDT</b> will be returned to:\n"
        f"<code>{address}</code>"
    )


def refund_sent(order_id: int, usd: float, address: str, lang: str = "en") -> str:
    if lang == "hi":
        return (
            f"💸 <b>Order {tag(order_id)} ka refund bhej diya</b> — {usd:g} USDT:\n"
            f"<code>{address}</code> ✅"
        )
    return (
        f"💸 <b>Refund sent for order {tag(order_id)}</b> — {usd:g} USDT to:\n"
        f"<code>{address}</code> ✅"
    )


def ask_bank_new(lang: str = "en") -> str:
    if lang == "hi":
        return (
            "🏦 <b>Bank details ek hi message me</b> bhejein, har line me ek cheez:\n\n"
            "<code>Bank name\nAccount holder name\nAccount number\nIFSC</code>\n\n"
            "<b>My Bank Cards</b> me save ho jayega — agli baar bas select karna hai."
        )
    return (
        "🏦 Send the <b>bank details in one message</b>, one item per line:\n\n"
        "<code>Bank name\nAccount holder name\nAccount number\nIFSC</code>\n\n"
        "It's saved to <b>My Bank Cards</b> so next time you just pick it."
    )


def proof_post(order_id: int, usd: float, inr: float, service_label: str,
               minutes: int) -> str:
    """Anonymized completion proof for the public channel — no names, no banks."""
    return (
        f"✅ <b>Order {tag(order_id)} completed</b>\n"
        f"💵 {usd:g}$ USDT → ₹{inr:,.0f} via {service_label}\n"
        f"⚡ Paid out in <b>{minutes} min</b> 🟢"
    )


def buy_soon(support: str, lang: str = "en") -> str:
    if lang == "hi":
        return ("🛒 <b>USDT Buy jald aa raha hai!</b>\n\n"
                f"Abhi kharidna hai? {html.escape(support)} ko message karein.")
    return ("🛒 <b>USDT Buy is opening soon!</b>\n\n"
            f"Want to buy right now? Message {html.escape(support)} and we'll sort you out.")


def support_text(support: str, lang: str = "en") -> str:
    if lang == "hi":
        return (f"🆘 Kisi order me dikkat? {html.escape(support)} ko message karein "
                "aur apna order ID batayein (jaise <code>#ORD12</code>).")
    return (f"🆘 Any issue with an order? Message {html.escape(support)} "
            "and mention your order ID (like <code>#ORD12</code>).")


def language_saved(lang: str) -> str:
    if lang == "hi":
        return "🌐 Bhasha set: <b>Hinglish</b> — ab se messages Hinglish me aayenge."
    return "🌐 Language set: <b>English</b>."


CHOOSE_LANGUAGE = ("🌐 Choose your language / Apni bhasha chunein:")

DESK_CLOSED = "The desk is closed right now — please check back soon."
BANNED = "Your account is blocked. Contact support."
