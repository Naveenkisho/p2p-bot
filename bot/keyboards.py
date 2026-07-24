from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .config import SERVICES
from .models import BankCard

CANCEL_TEXT = "❌ Cancel"


def cancel_kb() -> ReplyKeyboardMarkup:
    """Bottom reply-keyboard shown only while the user is mid-task, so there's
    always a one-tap way out of the current step."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=CANCEL_TEXT)]],
        resize_keyboard=True, one_time_keyboard=False,
        input_field_placeholder="Type here, or tap ❌ Cancel")


def hide_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


class OrderCb(CallbackData, prefix="o"):
    """User actions on their own order."""

    action: str  # check | cancel
    order_id: int


class AdminCb(CallbackData, prefix="a"):
    """Admin actions on an order."""

    action: str  # done | refunded | reject_refund | claim_ok | claim_no
    order_id: int


class ClaimReqCb(CallbackData, prefix="clm"):
    """User taps 'I already sent USDT' to claim a payment auto-detect missed."""

    order_id: int


class PickBankCb(CallbackData, prefix="pb"):
    """Bank pick after the deposit is confirmed (card_id=0 → add new)."""

    order_id: int
    card_id: int


class PreBankCb(CallbackData, prefix="pbk"):
    """Bank pick during checkout, before the order exists (card_id=0 → add new)."""

    card_id: int


class BankRmCb(CallbackData, prefix="brm"):
    card_id: int


class RefundReqCb(CallbackData, prefix="rfq"):
    """User taps 'Request refund' on a cancelled order."""

    order_id: int


def request_refund_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ Request refund (I already sent USDT)",
                             callback_data=RefundReqCb(order_id=order_id).pack())]])


def start_fresh_kb() -> InlineKeyboardMarkup:
    """Shown when a deposit session expires — one tap to begin a fresh payout
    (new address + amount at the current rate)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💵 Start a fresh payout", callback_data="menu:sell")]])


def _claim_btn(order_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="✅ I already sent USDT — confirm it",
        callback_data=ClaimReqCb(order_id=order_id).pack())


def expired_kb(order_id: int) -> InlineKeyboardMarkup:
    """Expired deposit: start over, or claim a payment that landed late."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 Start a fresh payout", callback_data="menu:sell")],
        [_claim_btn(order_id)],
    ])


def cancelled_kb(order_id: int) -> InlineKeyboardMarkup:
    """Cancelled order: claim a payment already sent, or ask for a refund."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [_claim_btn(order_id)],
        [InlineKeyboardButton(text="↩️ Refund me instead (I want my USDT back)",
                              callback_data=RefundReqCb(order_id=order_id).pack())],
    ])


def not_detected_kb(order_id: int) -> InlineKeyboardMarkup:
    """After a check finds nothing: re-check, claim with a TXID, or cancel."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Check status",
                              callback_data=OrderCb(action="check", order_id=order_id).pack())],
        [_claim_btn(order_id)],
        [InlineKeyboardButton(text="❌ Cancel order",
                              callback_data=OrderCb(action="cancel", order_id=order_id).pack())],
    ])


def claim_review_kb(order_id: int) -> InlineKeyboardMarkup:
    """Admin review of a user's payment claim."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Confirm payment — queue payout",
                              callback_data=AdminCb(action="claim_ok", order_id=order_id).pack())],
        [InlineKeyboardButton(text="🚫 Reject (can't verify)",
                              callback_data=AdminCb(action="claim_no", order_id=order_id).pack())],
    ])


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💵 USDT Sell", callback_data="menu:sell"),
            InlineKeyboardButton(text="🛒 USDT Buy", callback_data="menu:buy"),
        ],
        [InlineKeyboardButton(text="🛡 100% Clean Funds — Guarantee",
                              callback_data="menu:guarantee")],
        [
            InlineKeyboardButton(text="📋 My Orders", callback_data="menu:orders"),
            InlineKeyboardButton(text="🏦 My Bank Cards", callback_data="menu:banks"),
        ],
        [
            InlineKeyboardButton(text="📈 Rates", callback_data="menu:rates"),
            InlineKeyboardButton(text="🆘 Support", callback_data="menu:support"),
        ],
        [InlineKeyboardButton(text="🌐 Language / Bhasha", callback_data="menu:lang")],
    ])


def with_back(kb: InlineKeyboardMarkup | None = None) -> InlineKeyboardMarkup:
    """Append a '⬅️ Back to menu' row to any inline keyboard (or make one)."""
    rows = list(kb.inline_keyboard) if kb else []
    rows.append([InlineKeyboardButton(text="⬅️ Back to menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bot_link_kb(username: str | None) -> InlineKeyboardMarkup | None:
    """Tap-to-open-bot button shown under every proof-channel post."""
    if not username:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💵 Start Trading — Sell USDT",
                             url=f"https://t.me/{username}")]])


def support_row_kb(handles: list[str]) -> InlineKeyboardMarkup | None:
    """Support contacts as tap-to-chat buttons laid out horizontally (up to 3
    per row) — a clean, straight row of contacts."""
    btns = [
        InlineKeyboardButton(text=f"💬 {h}", url=f"https://t.me/{h.lstrip('@')}")
        for h in handles if h.lstrip("@")
    ]
    if not btns:
        return None
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def language_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
            InlineKeyboardButton(text="🇮🇳 Hinglish", callback_data="lang:hi"),
        ],
    ])


PANEL_TABS = {
    "active": "💰 Active",
    "pending": "⏳ Pending",
    "refunds": "↩️ Refunds",
    "done": "✅ Done",
}


def panel_kb(active_tab: str) -> InlineKeyboardMarkup:
    keys = list(PANEL_TABS.items())
    # two tabs per row so 4 fit cleanly, then a refresh row
    rows = [
        [InlineKeyboardButton(
            text=("• " + label if key == active_tab else label),
            callback_data=f"tab:{key}") for key, label in keys[i:i + 2]]
        for i in range(0, len(keys), 2)
    ]
    rows.append([InlineKeyboardButton(text="🔄 Refresh", callback_data=f"tab:{active_tab}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def services_kb(rates: dict[str, float]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{SERVICES[key]} — 1$/₹{rate:g}",
                              callback_data=f"svc:{key}")]
        for key, rate in rates.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pre_bank_chooser_kb(cards: list[BankCard]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🏦 {c.label}",
                              callback_data=PreBankCb(card_id=c.id).pack())]
        for c in cards
    ]
    rows.append([InlineKeyboardButton(
        text="➕ Add new bank", callback_data=PreBankCb(card_id=0).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bank_chooser_kb(order_id: int, cards: list[BankCard]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🏦 {c.label}",
                              callback_data=PickBankCb(order_id=order_id, card_id=c.id).pack())]
        for c in cards
    ]
    rows.append([InlineKeyboardButton(
        text="➕ Add new bank", callback_data=PickBankCb(order_id=order_id, card_id=0).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def banks_menu_kb(cards: list[BankCard]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🗑 Remove {c.label}", callback_data=BankRmCb(card_id=c.id).pack())]
        for c in cards
    ]
    rows.append([InlineKeyboardButton(text="➕ Add new bank", callback_data="banks:add")])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def deposit_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Check status",
                              callback_data=OrderCb(action="check", order_id=order_id).pack())],
        [InlineKeyboardButton(text="❌ Cancel order",
                              callback_data=OrderCb(action="cancel", order_id=order_id).pack())],
    ])


def admin_order_kb(order_id: int, status: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if status == "pending_payout":
        rows.append([InlineKeyboardButton(
            text="✅ Done — INR sent",
            callback_data=AdminCb(action="done", order_id=order_id).pack())])
    elif status == "refund_requested":
        rows.append([InlineKeyboardButton(
            text="💸 Refund sent (to sender)",
            callback_data=AdminCb(action="refunded", order_id=order_id).pack())])
        rows.append([InlineKeyboardButton(
            text="🚫 Reject (fake / no deposit)",
            callback_data=AdminCb(action="reject_refund", order_id=order_id).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)
