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

    action: str  # done | refunded
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


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💵 USDT Sell", callback_data="menu:sell"),
            InlineKeyboardButton(text="🛒 USDT Buy", callback_data="menu:buy"),
        ],
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


def language_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
            InlineKeyboardButton(text="🇮🇳 Hinglish", callback_data="lang:hi"),
        ],
    ])


PANEL_TABS = {
    "active": "⏳ Active",
    "refunds": "↩️ Refunds",
    "done": "✅ Done",
}


def panel_kb(active_tab: str) -> InlineKeyboardMarkup:
    tabs = [
        InlineKeyboardButton(
            text=("• " + label if key == active_tab else label),
            callback_data=f"tab:{key}")
        for key, label in PANEL_TABS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        tabs,
        [InlineKeyboardButton(text="🔄 Refresh", callback_data=f"tab:{active_tab}")],
    ])


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
    elif status in ("cancelled", "refund_requested"):
        rows.append([InlineKeyboardButton(
            text="💸 Refund sent",
            callback_data=AdminCb(action="refunded", order_id=order_id).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)
