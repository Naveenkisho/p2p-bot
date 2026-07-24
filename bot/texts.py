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
    Telegram search for #ORD0012 finds the whole trail. Zero-padded to 4
    digits, then grows naturally (#ORD9999 → #ORD10000) forever."""
    return f"#ORD{order_id:04d}"


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
            "🇮🇳 <b>India ki sabse safe P2P trading.</b>\n"
            "💯 <b>100% clean funds · zero freeze risk</b> — tap 🛡 Guarantee niche.\n\n"
            "Settle karte hain <b>UPI · IMPS instant · CDM · Cheque transfer</b> se — "
            "har service ka apna live rate.\n\n"
            "Kya karna chahenge?"
        )
    return (
        f"👋 Welcome, <b>{who}</b>!\n"
        f"🆔 Your ID: <code>{user_id}</code>\n\n"
        "🇮🇳 <b>The safest P2P trading in entire India.</b>\n"
        "💯 <b>100% clean funds · zero freeze risk</b> — tap 🛡 Guarantee below.\n\n"
        "We settle through <b>UPI · IMPS instant · CDM · Cheque transfer</b> — "
        "each with its own live rate.\n\n"
        "What would you like to do?"
    )


def guarantee(lang: str = "en") -> str:
    if lang == "hi":
        return (
            "🛡 <b>100% Clean Funds — Hamari Guarantee</b>\n\n"
            "Har payout <b>verified, legal source</b> se aata hai:\n\n"
            "✅ Mutual &amp; stock-market funds\n"
            "✅ Cash-deposit funds\n"
            "✅ Credit-card funds\n"
            "✅ Payment-gateway funds\n\n"
            "Sab <b>100% clean, legally sourced</b> paisa — aapke account ko kabhi "
            "freeze/hold ka risk nahi. Ye hamari <b>commitment</b> hai: hamare saath "
            "trade karein aur <b>zindagi bhar freeze ki tension bhulein.</b>\n\n"
            "📸 Har deal par <b>payout proof</b> bhejte hain.\n"
            "🔒 Admins khud personally handle karte hain.\n"
            "⚡ Auto-verified deposits · fast payout.\n"
            "🇮🇳 India ka sabse safe P2P desk."
        )
    return (
        "🛡 <b>100% Clean Funds — Our Guarantee</b>\n\n"
        "Every rupee we pay out comes from <b>verified, legitimate sources</b>:\n\n"
        "✅ Mutual &amp; stock-market funds\n"
        "✅ Cash-deposit funds\n"
        "✅ Credit-card funds\n"
        "✅ Payment-gateway funds\n\n"
        "All <b>100% clean, legally sourced</b> money — so your account is "
        "<b>never</b> at risk of a freeze or hold. That's our commitment: trade "
        "with us and <b>never worry about fund-freeze issues in your entire life.</b>\n\n"
        "📸 We share a <b>payout proof</b> on every single deal.\n"
        "🔒 Every order handled personally by our admins.\n"
        "⚡ Deposits auto-verified on-chain · fast payout.\n"
        "🇮🇳 The safest P2P desk in India."
    )


def support_msg(lang: str = "en") -> str:
    if lang == "hi":
        return (
            "🆘 <b>Support</b>\n\n"
            "Kisi bhi help ke liye niche apne <b>support contact</b> par tap karein — "
            "apna order ID (<code>#ORD…</code>) zaroor batayein.\n\n"
            "⚡ Fast reply · 🔒 Verified admins · 📸 Proof on request"
        )
    return (
        "🆘 <b>Support</b>\n\n"
        "Tap your <b>support contact</b> below for any help — always mention your "
        "order ID (<code>#ORD…</code>).\n\n"
        "⚡ Fast replies · 🔒 Verified admins · 📸 Proof on request"
    )


def services_header(rates: dict[str, float], lang: str = "en") -> str:
    head = ("💵 <b>USDT Sell</b> — payout method chunein:" if lang == "hi"
            else "💵 <b>Sell USDT</b> — choose payout method:")
    lines = [head, ""]
    for key, rate in rates.items():
        lines.append(f"• {SERVICES[key]} — <b>1$ = ₹{rate:g}</b>")
    return "\n".join(lines)


def ask_amount(service_label: str, rate: float, lo: float, hi: float,
               lang: str = "en") -> str:
    if lang == "hi":
        return (f"<b>{service_label}</b> · 1$ = ₹{rate:g}\n\n"
                f"Kitne <b>$</b>? ({lo:g}–{hi:g}) — bas number bhejein:")
    return (f"<b>{service_label}</b> · 1$ = ₹{rate:g}\n\n"
            f"Enter amount in <b>$</b> ({lo:g}–{hi:g}):")


def choose_bank(usd: float, inr: float, lang: str = "en") -> str:
    if lang == "hi":
        return (f"<b>{usd:g}$ → ₹{inr:,.2f}</b>\n\n🏦 Payout bank chunein:")
    return (f"<b>{usd:g}$ → ₹{inr:,.2f}</b>\n\n🏦 Choose your payout bank:")


def rate_updated_note(rate: float, lang: str = "en") -> str:
    if lang == "hi":
        return f"📈 Aapke quote ke baad rate update hua: <b>1$ / ₹{rate:g}</b>\n\n"
    return f"📈 Rate updated since your quote: <b>1$ / ₹{rate:g}</b>\n\n"


def deposit_request(order_id: int, usd: float, inr: float, service_label: str,
                    address: str, rate: float, rate_note: str = "",
                    bank_label: str = "", lang: str = "en") -> str:
    bank = html.escape(bank_label) if bank_label else service_label
    if lang == "hi":
        return (
            f"💸 Bhejein <b>exactly {usd:g} USDT</b> (TRC20) is address par:\n"
            f"<code>{address}</code>\n\n"
            f"⏱ Auto-verify — transfer confirm hote hi, usually <b>10–20 second</b> me.\n"
            f"⚠️ Sirf TRC20 · exact amount · {settings.deposit_ttl_min} min me expire\n\n"
            f"{rate_note}"
            f"💵 Aapko milenge <b>₹{inr:,.2f}</b> → {bank}\n"
            f"🧾 Ref: <code>{tag(order_id)}</code>"
        )
    return (
        f"💸 Send <b>exactly {usd:g} USDT</b> (TRC20) to:\n"
        f"<code>{address}</code>\n\n"
        f"⏱ Auto-verified — usually <b>10–20 seconds</b> after your transfer confirms.\n"
        f"⚠️ TRC20 only · exact amount only · expires in {settings.deposit_ttl_min} min\n\n"
        f"{rate_note}"
        f"💵 You'll receive <b>₹{inr:,.2f}</b> → {bank}\n"
        f"🧾 Ref: <code>{tag(order_id)}</code>"
    )


def queue_short(position: int, lang: str = "en") -> str:
    if position <= 1:
        return "🚀 first in queue" if lang == "en" else "🚀 queue me pehle"
    return f"queue <b>#{position}</b>"


def deposit_verified(order_id: int, usd: float, inr: float, txid: str,
                     bank_label: str, position: int, lang: str = "en") -> str:
    tx = f"🔗 <code>{html.escape(txid)}</code>\n" if txid and txid != "manual" else ""
    if lang == "hi":
        return (
            f"✅ <b>{usd:g} USDT mil gaye — verified!</b>\n{tx}\n"
            f"💵 <b>₹{inr:,.2f}</b> → {html.escape(bank_label)}\n"
            f"⏱ Payout <b>{settings.eta_text}</b> me · {queue_short(position, lang)}\n"
            f"🧾 Ref: <code>{tag(order_id)}</code>\n\n"
            "Relax karein — funds aa rahe hain. 🟢"
        )
    return (
        f"✅ <b>{usd:g} USDT received — verified!</b>\n{tx}\n"
        f"💵 <b>₹{inr:,.2f}</b> → {html.escape(bank_label)}\n"
        f"⏱ Payout within <b>{settings.eta_text}</b> · {queue_short(position, lang)}\n"
        f"🧾 Ref: <code>{tag(order_id)}</code>\n\n"
        "Relax — your funds are on the way. 🟢"
    )


def deposit_received(order_id: int, usd: float, inr: float, txid: str,
                     lang: str = "en", ask_bank: bool = True) -> str:
    tx_note = f"🔗 TX: <code>{html.escape(txid)}</code>\n" \
        if txid and txid != "manual" else ""
    if lang == "hi":
        base = (
            f"✅✅ <b>Aapke {usd:g} USDT mil gaye — verified!</b> — Order {tag(order_id)} 🟢\n"
            f"{tx_note}\n"
            f"Aapka payout: <b>₹{inr:,.2f}</b>"
        )
        if ask_bank:
            base += ("\n\n🏦 Last step — niche apna <b>bank chunein</b> (ya naya "
                     "add karein), funds turant process honge.")
        return base
    base = (
        f"✅✅ <b>We received your {usd:g} USDT — verified!</b> — Order {tag(order_id)} 🟢\n"
        f"{tx_note}\n"
        f"Your payout: <b>₹{inr:,.2f}</b>"
    )
    if ask_bank:
        base += ("\n\n🏦 Final step — <b>choose your bank</b> below (or add one) "
                 "and your funds are on the way.")
    return base


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


def deposit_reminder(order_id: int, usd: float, address: str,
                     lang: str = "en") -> str:
    if lang == "hi":
        return (
            f"⏳ <b>Order {tag(order_id)} abhi pending hai</b>\n\n"
            f"Complete karne ke liye bhejein <b>exactly {usd:g} USDT</b> (TRC20):\n"
            f"<code>{address}</code>\n\n"
            "⚡ Auto-verify seconds me. Bhej diya? Niche <b>🔍 Check status</b> dabayein."
        )
    return (
        f"⏳ <b>Order {tag(order_id)} is still pending</b>\n\n"
        f"To complete it, send <b>exactly {usd:g} USDT</b> (TRC20) to:\n"
        f"<code>{address}</code>\n\n"
        "⚡ Auto-verified in seconds. Already sent? Tap <b>🔍 Check status</b> below."
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
            "🏦 <b>Bank details bhejein</b> — is format me, har line me ek:\n\n"
            "<code>Bank: Axis\n"
            "Name: Ravi Kumar\n"
            "A/c: 1234567890\n"
            "IFSC: UTIB0001234</code>\n\n"
            "Copy karke apne details bhar dein. <b>My Bank Cards</b> me save rahega."
        )
    return (
        "🏦 <b>Send your bank details</b> in this format — one per line:\n\n"
        "<code>Bank: Axis\n"
        "Name: Ravi Kumar\n"
        "A/c: 1234567890\n"
        "IFSC: UTIB0001234</code>\n\n"
        "Just copy and fill in yours. Saved to <b>My Bank Cards</b> for next time."
    )


def proof_post(order_id: int, usd: float, rate: float, inr: float,
               service_label: str, minutes: int) -> str:
    """Anonymized completion proof for the public channel. ONLY: order tag,
    USDT amount, rate, INR paid, service, speed. Never names, usernames, IDs,
    bank details, deposit addresses or tx hashes."""
    return (
        f"✅ <b>Order {tag(order_id)} completed</b>\n"
        f"💵 Sold: <b>{usd:g}$ USDT</b> @ 1$/₹{rate:g}\n"
        f"🏦 Paid: <b>₹{inr:,.0f}</b> via {service_label}\n"
        f"⚡ Done in <b>{minutes} min</b> 🟢\n"
        f"🛡 <b>100% clean funds only</b> — zero freeze risk\n"
        f"📈 Rates change fast — <b>order now!</b>"
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
