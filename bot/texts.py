"""Every user-facing message in one place, in English ("en") and Roman-Hindi
Hinglish ("hi"). Functions take `lang` as the last argument and fall back to
English. User-controlled values (names, bank details, labels) are HTML-escaped
HERE, so callers pass them raw. Admin-side texts stay English by design.
"""

import html
from datetime import timedelta

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
    "refund_rejected": "🚫",
}


def tag(order_id: int) -> str:
    """Search-friendly order tag — same on the user and admin side, so one
    Telegram search for #ORD0012 finds the whole trail. Zero-padded to 4
    digits, then grows naturally (#ORD9999 → #ORD10000) forever."""
    return f"#ORD{order_id:04d}"


def usd_str(amount: float) -> str:
    """Exact deposit amount for display: 2 decimals, trailing zeros trimmed
    (100 → "100", 100.02 → "100.02", 10000.5 → "10000.5"). Unlike "%g" this
    never rounds past 6 significant figures, so the number the customer is
    told to send always equals the amount the scanner matches on."""
    return f"{amount:.2f}".rstrip("0").rstrip(".")


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


def ask_network(lang: str = "en") -> str:
    if lang == "hi":
        return ("🌐 <b>Kaunse network par USDT bhejenge?</b>\n\n"
                "🔷 <b>TRC20</b> (TRON) · 🟡 <b>BEP20</b> (BSC)\n"
                "Chun lein — hum usi ka address denge 👇")
    return ("🌐 <b>Which network will you send USDT on?</b>\n\n"
            "🔷 <b>TRC20</b> (TRON) · 🟡 <b>BEP20</b> (BSC)\n"
            "Pick one — we'll show that network's address 👇")


def ask_amount(service_label: str, rate: float, lo: float, hi: float,
               lang: str = "en") -> str:
    if lang == "hi":
        return (f"<b>{service_label}</b> · 1$ = ₹{rate:g}\n\n"
                f"Kitne <b>$</b>? ({lo:g}–{hi:g}) — bas number bhejein:")
    return (f"<b>{service_label}</b> · 1$ = ₹{rate:g}\n\n"
            f"Enter amount in <b>$</b> ({lo:g}–{hi:g}):")


def choose_bank(usd: float, inr: float, service_label: str, rate: float,
                has_bank: bool, lang: str = "en") -> str:
    if lang == "hi":
        head = (f"💵 Aap bhejte hain: <b>{usd_str(usd)}$ USDT</b>\n"
                f"📊 Method: <b>{service_label}</b> · 1$ = ₹{rate:g}\n"
                f"💰 Aapko milenge: <b>₹{inr:,.2f}</b>\n\n")
        head += ("🏦 <b>Apna payout bank chunein 👇</b>" if has_bank
                 else "🏦 <b>Koi bank saved nahi — continue karne ke liye niche "
                      "apni bank details add karein:</b>")
        return head
    head = (f"💵 You send: <b>{usd_str(usd)}$ USDT</b>\n"
            f"📊 Method: <b>{service_label}</b> · 1$ = ₹{rate:g}\n"
            f"💰 You receive: <b>₹{inr:,.2f}</b>\n\n")
    head += ("🏦 <b>Choose your payout bank 👇</b>" if has_bank
             else "🏦 <b>No bank saved yet — add your bank details below to continue:</b>")
    return head


def rate_updated_note(rate: float, lang: str = "en") -> str:
    if lang == "hi":
        return f"📈 Aapke quote ke baad rate update hua: <b>1$ / ₹{rate:g}</b>\n\n"
    return f"📈 Rate updated since your quote: <b>1$ / ₹{rate:g}</b>\n\n"


def _amount_box(amt: str) -> str:
    """The exact amount inside a real box — top & bottom rules with corners — but
    NOT a <pre>/code block (so no code badge, nothing to mis-copy). The sides are
    open so it lines up in Telegram's normal proportional font; the amount is bold
    and shifted toward the middle."""
    rule = "━" * 13
    return (f"┏{rule}┓\n"
            f"      💠 <b>{amt} USDT</b>\n"
            f"┗{rule}┛")


def support_footer(support: str, lang: str = "en") -> str:
    """A one-line 'need help?' footer, shown on every customer status message so
    support is always one tap away."""
    s = html.escape(support)
    if lang == "hi":
        return ("\n\n━━━━━━━━━━━━━━\n"
                f"🆘 <b>Koi dikkat?</b> {s} ko message karein — minutes me reply.")
    return ("\n\n━━━━━━━━━━━━━━\n"
            f"🆘 <b>Need help?</b> Message {s} — we usually reply within minutes.")


def _ist_hm(dt) -> str:
    """A naive-UTC datetime as IST HH:MM."""
    return (dt + timedelta(hours=5, minutes=30)).strftime("%H:%M") if dt else ""


def deposit_request(order_id: int, usd: float, inr: float, service_label: str,
                    address: str, net_label: str, rate: float, created_at=None,
                    rate_note: str = "", bank_label: str = "", support: str = "",
                    ttl_min: int | None = None, lang: str = "en") -> str:
    """Deposit screen for ONE chosen network. The address sits ABOVE the amount box
    (never the last line), so tapping it to copy can't accidentally hit the inline
    buttons; the amount box is bold/centered, not a code block. A QR is sent along."""
    bank = html.escape(bank_label) if bank_label else service_label
    amt = usd_str(usd)
    dec = f".{amt.split('.')[1]}" if "." in amt else ""
    amt_box = _amount_box(amt)
    ttl = ttl_min or settings.deposit_ttl_min
    net = html.escape(net_label)
    net_emoji = "🟡" if "BEP20" in net_label else "🔷"
    addr = html.escape(address)
    times = ""
    if created_at is not None:
        times = f"🕐 {_ist_hm(created_at)} → expires {_ist_hm(created_at + timedelta(minutes=ttl))} IST\n"
    if lang == "hi":
        sup = f"🆘 <b>Koi dikkat?</b> {html.escape(support)}\n" if support else ""
        warn = (f"❗ <b>{dec} ke saath bhejein</b> — bilkul exact amount, decimals samet. "
                "Galat amount auto-detect nahi ho sakta." if dec else
                "❗ <b>Bilkul exact amount</b> bhejein.")
        return (
            f"📥 <b>USDT Deposit</b> · <code>{tag(order_id)}</code>\n\n"
            f"💵 Aapko milenge <b>₹{inr:,.2f}</b> → {bank}\n"
            f"{times}"
            f"⏱ Confirm hote hi seconds me auto-verify\n"
            f"{sup}{rate_note}"
            "━━━━━━━━━━━━━━\n"
            f"{net_emoji} <b>{net}</b> — address copy karein 👇\n"
            f"<code>{addr}</code>\n\n"
            f"💸 Phir <b>exactly itna</b> bhejein 👇\n{amt_box}\n"
            f"{warn}"
        )
    sup = f"🆘 <b>Need help?</b> {html.escape(support)}\n" if support else ""
    warn = (f"❗ <b>Include the {dec}</b> — send the EXACT amount, decimals and all. "
            "A wrong amount may not auto-detect." if dec else
            "❗ <b>Send the exact amount.</b>")
    return (
        f"📥 <b>USDT Deposit</b> · <code>{tag(order_id)}</code>\n\n"
        f"💵 You'll receive <b>₹{inr:,.2f}</b> → {bank}\n"
        f"{times}"
        f"⏱ Auto-verified in seconds after it confirms\n"
        f"{sup}{rate_note}"
        "━━━━━━━━━━━━━━\n"
        f"{net_emoji} On <b>{net}</b> — tap the address to copy 👇\n"
        f"<code>{addr}</code>\n\n"
        f"💸 Then send <b>exactly</b> 👇\n{amt_box}\n"
        f"{warn}"
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
            "Aapne bataya aapne pay nahi kiya — kuch pending nahi hai. Kabhi bhi "
            "naya order shuru karein.\n\n"
            "<i>Galti se USDT bhej diya tha? Niche support ko apna order ID aur "
            "TXID bhejein — hum sort kar denge.</i>"
        )
    return (
        f"❌ <b>Order {tag(order_id)} cancelled.</b>\n\n"
        "You said you haven't paid — so nothing's pending. Start a fresh order "
        "anytime.\n\n"
        "<i>Sent the USDT by mistake? Message support (below) with your order ID and "
        "TXID and we'll sort it out.</i>"
    )


def ask_refund_txid(order_id: int, lang: str = "en") -> str:
    if lang == "hi":
        return (
            f"↩️ <b>Refund — Order {tag(order_id)}</b>\n\n"
            "Apne USDT deposit ka <b>TXID</b> (transaction hash) yahan paste karein.\n\n"
            "🔒 Refund <b>usi wallet</b> me jayega jahan se USDT aaya tha — isliye "
            "hum address nahi maangte. Aap loot nahi sakte, aur na koi aur. "
            "Tronscan par verify karke bhejenge."
        )
    return (
        f"↩️ <b>Refund — Order {tag(order_id)}</b>\n\n"
        "Paste the <b>TXID</b> (transaction hash) of your USDT deposit here.\n\n"
        "🔒 The refund goes back to the <b>exact wallet you sent from</b> — that's why "
        "we never ask for an address. We verify the TXID on Tronscan and return it."
    )


def refund_submitted(order_id: int, lang: str = "en") -> str:
    if lang == "hi":
        return (
            f"✅ <b>Refund request submit ho gaya — Order {tag(order_id)}</b>\n\n"
            "Hamari team TXID verify karegi aur USDT usi wallet me wapas bhejegi "
            "jahan se aaya tha. Thoda intezar karein. 🙏"
        )
    return (
        f"✅ <b>Refund request submitted — Order {tag(order_id)}</b>\n\n"
        "Our team will verify the TXID and send the USDT back to the wallet it "
        "came from. Please allow a little time. 🙏"
    )


def payment_not_detected(order_id: int, support: str, lang: str = "en") -> str:
    if lang == "hi":
        return (
            f"❌ <b>Order {tag(order_id)} ka exact amount abhi tak nahi mila.</b>\n\n"
            "🎯 Galti se <b>alag amount</b> bhej diya? (platform fee kat gayi ho sakti hai)\n"
            "👉 Niche <b>“🧾 Bhej diya — TXID submit karein”</b> dabayein aur apna "
            "transaction hash paste karein. Hum <b>manually verify</b> karke "
            "<b>~10 min</b> me payout kar denge.\n\n"
            "Abhi tak bheja hi nahi? Upar diye address par <b>exact amount</b> bhejein, "
            "ya <b>❌ Cancel order</b> dabayein." + support_footer(support, lang)
        )
    return (
        f"❌ <b>We haven't received the exact amount for Order {tag(order_id)} yet.</b>\n\n"
        "🎯 Sent a <b>different amount</b> by mistake? (a platform fee may have been deducted)\n"
        "👉 Tap <b>“🧾 I've sent it — submit TXID”</b> below and paste your transaction "
        "hash. We'll <b>verify it manually</b> and pay out within <b>~10 min</b>.\n\n"
        "Haven't sent it yet? Send the <b>exact amount</b> to the address above, or tap "
        "<b>❌ Cancel order</b>." + support_footer(support, lang)
    )


def checking_now(lang: str = "en") -> str:
    return ("🔍 Check kar rahe hain — chat dekhein 👇" if lang == "hi"
            else "🔍 Checking — see your chat 👇")


def checking_wait(lang: str = "en") -> str:
    if lang == "hi":
        return (
            "🔍 <b>Blockchain check kar rahe hain…</b>\n\n"
            "Please <b>~60 second wait</b> karein — fresh transfer ko confirm hone me "
            "lagbhag 1 minute lagta hai. Result yahi bhej denge. ⏳"
        )
    return (
        "🔍 <b>Checking the blockchain…</b>\n\n"
        "Please <b>wait ~60 seconds</b> — a fresh transfer takes about a minute to "
        "confirm on-chain. We'll send the result here. ⏳"
    )


def deposit_reminder(order_id: int, usd: float, address: str,
                     net_label: str = "TRC20 (TRON)", lang: str = "en",
                     support: str = "", ttl_min: int | None = None) -> str:
    amt = usd_str(usd)
    amt_box = _amount_box(amt)
    net = html.escape(net_label)
    dec = f"🎯 decimals bhi (.{amt.split('.')[1]})\n" if "." in amt else ""
    dec_en = f"🎯 include the decimals (.{amt.split('.')[1]})\n" if "." in amt else ""
    left = max(1, (ttl_min or settings.deposit_ttl_min) - settings.remind_min)
    foot = support_footer(support, lang) if support else ""
    if lang == "hi":
        return (
            f"⏳ <b>Order {tag(order_id)} abhi pending hai</b>\n"
            f"⚠️ <b>Ye quote ~{left} min me expire ho jayega — abhi bhejein.</b>\n\n"
            f"Address ({net}):\n<pre>{html.escape(address)}</pre>\n"
            f"Send <b>exactly</b> 👇\n{amt_box}\n{dec}"
            "⚡ Auto-verify seconds me. Bhej diya? Niche <b>✅ I've sent it — check it</b> dabayein." + foot
        )
    return (
        f"⏳ <b>Order {tag(order_id)} is still pending</b>\n"
        f"⚠️ <b>This quote expires in ~{left} min — please send now.</b>\n\n"
        f"Address ({net}):\n<pre>{html.escape(address)}</pre>\n"
        f"Send <b>exactly</b> 👇\n{amt_box}\n{dec_en}"
        "⚡ Auto-verified in seconds. Already sent? Tap <b>✅ I've sent it — check it</b> below." + foot
    )


def quote_superseded(order_ids: list[int], lang: str = "en") -> str:
    """Shown when a customer starts a new order while an unpaid quote was live — the
    old quote is expired instantly so only one deposit screen is ever active."""
    tags = ", ".join(tag(o) for o in order_ids)
    if lang == "hi":
        return (
            f"♻️ <b>Purana quote {tags} cancel ho gaya</b> — aapne naya order shuru kiya.\n"
            f"Sirf niche wala <b>naya amount</b> hi bhejein.\n"
            f"<i>(Purana amount already bhej diya? Us order par 🧾 I've sent it dabakar claim karein.)</i>"
        )
    return (
        f"♻️ <b>Your earlier quote {tags} was cancelled</b> because you started a new order.\n"
        f"Please pay only the <b>new amount</b> shown below.\n"
        f"<i>(Already sent the old amount? Tap 🧾 I've sent it on that order to claim it.)</i>"
    )


def ask_claim_txid(order_id: int, lang: str = "en") -> str:
    if lang == "hi":
        return (
            f"✅ <b>Payment confirm karein — Order {tag(order_id)}</b>\n\n"
            "USDT bhej diya hai? Apna <b>TXID</b> (transaction hash — BEP20 par "
            "<code>0x…</code> se shuru hota hai) paste karein — hum use on-chain "
            "verify karke aapka payout confirm karenge.\n\n"
            "TXID aapko apne wallet ki transaction history me milega. "
            "Ya ❌ Cancel dabayein."
        )
    return (
        f"✅ <b>Confirm your payment — Order {tag(order_id)}</b>\n\n"
        "Already sent the USDT? Paste your <b>TXID</b> (the transaction hash — it "
        "starts with <code>0x…</code> on BEP20) — we'll verify it on-chain and "
        "confirm your payout.\n\n"
        "You'll find the TXID in your wallet's transaction history. "
        "Or tap ❌ Cancel."
    )


def claim_submitted(order_id: int, lang: str = "en") -> str:
    if lang == "hi":
        return (
            f"🔎 <b>Mil gaya — Order {tag(order_id)} review me hai.</b>\n\n"
            "Hum aapka TXID check kar rahe hain. Verify hote hi aapko payout "
            "confirmation yahi milega — usually kuch hi minute me. 🟢"
        )
    return (
        f"🔎 <b>Got it — Order {tag(order_id)} is under review.</b>\n\n"
        "We're checking your TXID. As soon as it's verified you'll get the "
        "payout confirmation right here — usually within minutes. 🟢"
    )


def claim_unverified_sent(order_id: int, lang: str = "en") -> str:
    """The TXID couldn't be seen on-chain (yet) — instead of refusing, it goes
    to the team as an UNVERIFIED claim. One per order."""
    if lang == "hi":
        return (
            f"🕵️ <b>Order {tag(order_id)} — manual verification me bheja gaya.</b>\n\n"
            "Hum abhi ye transaction on-chain nahi dekh paye, isliye humari team "
            "ise khud check karegi. Verify hote hi payout yahin confirm hoga — "
            "aapko kuch aur karne ki zaroorat nahi."
        )
    return (
        f"🕵️ <b>Order {tag(order_id)} — sent for manual verification.</b>\n\n"
        "We couldn't see that transaction on-chain just now, so our team will "
        "check it by hand. Once it verifies, your payout is confirmed right "
        "here — nothing more needed from you."
    )


def claim_already_manual(order_id: int, lang: str = "en") -> str:
    """A second unverifiable TXID on the same order — the first is already with
    the team; don't stack more."""
    if lang == "hi":
        return (
            f"⏳ Order {tag(order_id)} ka pehla TXID pehle se manual verification "
            "me hai — team check kar rahi hai. Agar aapko lagta hai NAYA TXID hi "
            "sahi hai to support ko bhejein."
        )
    return (
        f"⏳ Order {tag(order_id)} already has a TXID under manual verification — "
        "our team is checking it. If you believe this NEW hash is the right one, "
        "send it to support."
    )


def email_nudge(lang: str = "en") -> str:
    """One-time, after the first order: offer email receipts."""
    if lang == "hi":
        return ("📧 Order confirmation aur payout receipt <b>email par bhi</b> "
                "chahiye?\n<b>Bas apna email yahin bhej dein</b> — jaise "
                "<code>name@gmail.com</code>\nAgar wo email humare saath pehle "
                "se verified hai to turant chalu — koi code nahi.")
    return ("📧 Want your order confirmation and payout receipt <b>by email "
            "too</b>?\n<b>Just send your email address right here</b> — like "
            "<code>name@gmail.com</code>\nIf it's already verified with us it "
            "switches on instantly — no code needed.")


def claim_pick_order(lang: str = "en") -> str:
    """A hash was pasted with no order context — ask which order it pays for."""
    if lang == "hi":
        return ("🔎 <b>Ye TXID kis order ka hai?</b>\n\n"
                "Neeche apna order chunein — hum usi ke network par check karenge.")
    return ("🔎 <b>Which order is this TXID for?</b>\n\n"
            "Pick the order below — we'll check it on that order's network.")


def claim_pick_none(lang: str = "en") -> str:
    if lang == "hi":
        return ("Aapka koi order TXID ka wait nahi kar raha. /start dabakar "
                "apne orders dekhein — ya naya order banayein.")
    return ("None of your orders is waiting on a payment check. Press /start "
            "to see your orders — or create a new one.")


def _short_addr(addr: str) -> str:
    a = addr or ""
    return f"{a[:10]}…{a[-6:]}" if len(a) > 20 else a


def tx_wrong_address(to: str, our: str, lang: str = "en") -> str:
    """Declined on the spot: the submitted tx did NOT pay our deposit address."""
    if lang == "hi":
        return (
            "🚫 <b>Ye transaction hamare deposit address par nahi bheja gaya.</b>\n\n"
            f"➡️ Bheja gaya: <code>{html.escape(_short_addr(to))}</code>\n"
            f"✅ Hamara address: <code>{html.escape(_short_addr(our))}</code>\n\n"
            "Hum sirf usi USDT transfer ko verify/refund kar sakte hain jo aapke order "
            "ke deposit address par aaya ho. Sahi TXID check karke dobara bhejein."
        )
    return (
        "🚫 <b>This transaction was NOT sent to our deposit address.</b>\n\n"
        f"➡️ It was sent to: <code>{html.escape(_short_addr(to))}</code>\n"
        f"✅ Our address is: <code>{html.escape(_short_addr(our))}</code>\n\n"
        "We can only verify or refund a USDT transfer made to your order's deposit "
        "address. Double-check the TXID and try again."
    )


def tx_not_found(lang: str = "en") -> str:
    if lang == "hi":
        return (
            "🚫 <b>Ye transaction on-chain nahi mila.</b>\n\n"
            "Abhi bheja hai? ~1 min ruk kar dobara try karein. Warna TXID check karein — "
            "wo hamare address par ek confirmed USDT transfer hona chahiye."
        )
    return (
        "🚫 <b>We couldn't find that transaction on-chain.</b>\n\n"
        "Just sent it? Wait ~1 min and try again. Otherwise check the TXID — it must be "
        "a confirmed USDT transfer to your order's deposit address."
    )


def tx_amount_mismatch(actual: float, expected: float, lang: str = "en") -> str:
    """Declined: the tx amount doesn't match THIS order's amount — so it can't be this
    order's payment (blocks claiming another customer's mismatched deposit)."""
    if lang == "hi":
        return (
            f"🚫 <b>Ye transaction {usd_str(actual)} USDT ka hai, par ye order "
            f"{usd_str(expected)} USDT ka hai.</b>\n\n"
            "Ise is order ka payment nahi maana ja sakta. Jo transfer aapne <b>is order</b> "
            "ke liye kiya hai uska TXID bhejein — ya support se baat karein."
        )
    return (
        f"🚫 <b>That transaction is for {usd_str(actual)} USDT, but this order is for "
        f"{usd_str(expected)} USDT.</b>\n\n"
        "It can't be the payment for this order. Please submit the TXID of the transfer you "
        "made for <b>this</b> order — or contact support."
    )


def tx_too_old(days: int, lang: str = "en") -> str:
    if lang == "hi":
        return (
            f"🚫 <b>Ye transaction {days} din purana hai</b> — ye is order ka payment "
            "nahi ho sakta. Is order ke liye jo transfer kiya hai uska TXID bhejein."
        )
    return (
        f"🚫 <b>That transaction is {days} days old</b> — it can't be the payment for "
        "this order. Please submit the TXID of the transfer you made for THIS order."
    )


def claim_rejected(order_id: int, support: str, lang: str = "en") -> str:
    if lang == "hi":
        return (
            f"❌ <b>Order {tag(order_id)} — payment verify nahi ho paya.</b>\n\n"
            "Us TXID par humein is order ka deposit nahi mila (galat amount, "
            "galat network, ya galat TXID ho sakta hai).\n\n"
            f"Aapko lagta hai ye galti hai? {html.escape(support)} ko apna "
            "<b>payment screenshot + TXID</b> bhejein — hum check karenge."
        )
    return (
        f"❌ <b>Order {tag(order_id)} — we couldn't verify that payment.</b>\n\n"
        "That TXID doesn't match this order's deposit (it may be the wrong "
        "amount, wrong network, or wrong TXID).\n\n"
        f"Think it's a mistake? Send your <b>payment screenshot + TXID</b> to "
        f"{html.escape(support)} and we'll take a look."
    )


def order_expired(order_id: int, support: str = "@support", lang: str = "en",
                  ttl_min: int | None = None) -> str:
    ttl = ttl_min or settings.deposit_ttl_min
    if lang == "hi":
        return (
            f"⌛ <b>Order {tag(order_id)} ka {ttl}-minute window "
            f"khatam ho gaya</b> — time par deposit nahi mila.\n\n"
            f"⚠️ <b>Ab us purane address/amount par kuch mat bhejein.</b>\n"
            f"Naya payout shuru karein 👇 — fresh address aur amount milega "
            f"(current rate par).\n\n"
            f"USDT already bhej diya tha? {html.escape(support)} ko order "
            f"{tag(order_id)} aur apna TXID bhejein — hum sort kar denge."
        )
    return (
        f"⌛ <b>Order {tag(order_id)}'s {ttl}-minute window has "
        f"closed</b> — no deposit arrived in time.\n\n"
        f"⚠️ <b>Don't send anything to that old address/amount now.</b>\n"
        f"Start a fresh payout 👇 — you'll get a new address and amount "
        f"(at the current rate).\n\n"
        f"Already sent the USDT? Message {html.escape(support)} with order "
        f"{tag(order_id)} and your TXID — we'll sort it out."
    )


def refund_rejected(order_id: int, support: str, lang: str = "en") -> str:
    if lang == "hi":
        return (
            f"🚫 <b>Order {tag(order_id)} — payment verify nahi hua.</b>\n\n"
            "Us TXID par humein aapke deposit ka koi record nahi mila — "
            "payment kabhi received nahi hua.\n\n"
            f"Aapko lagta hai ye hamari galti hai? Apna <b>payment screenshot + "
            f"sahi TXID</b> {html.escape(support)} ko bhejein — hum usually 5 min "
            "me reply karte hain."
        )
    return (
        f"🚫 <b>Order {tag(order_id)} — payment could not be verified.</b>\n\n"
        "We found no record of your deposit for that TXID — the payment was never "
        "received.\n\n"
        f"Still believe it's an error on our end? Send your <b>payment screenshot + "
        f"the correct TXID</b> to {html.escape(support)} — we usually reply within "
        "5 minutes."
    )


def refund_sent(order_id: int, lang: str = "en") -> str:
    if lang == "hi":
        return (
            f"💸 <b>Order {tag(order_id)} ka refund bhej diya</b> — USDT aapke "
            "sender wallet me wapas aa gaya. ✅"
        )
    return (
        f"💸 <b>Refund sent for order {tag(order_id)}</b> — the USDT has been "
        "returned to your sender wallet. ✅"
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
    speed = ("under a minute" if minutes <= 1 else f"{minutes} minutes")
    return (
        "🟢 <b>PAYOUT SETTLED</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"💰 <b>₹{inr:,.0f}</b> paid to bank\n"
        f"💱 {usd:g} USDT sold · <b>{service_label}</b>\n"
        f"📊 Locked rate: ₹{rate:g}/$\n"
        f"⚡ Settled in <b>{speed}</b>\n"
        "━━━━━━━━━━━━━━\n"
        "🛡 100% clean funds · zero freeze risk\n"
        f"🔖 Ref {tag(order_id)}\n\n"
        "💸 <b>Sell your USDT now</b> — INR in your bank in minutes."
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
