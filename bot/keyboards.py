from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import SERVICES
from .models import BankCard


class OrderCb(CallbackData, prefix="o"):
    """User actions on their own order."""

    action: str  # sent | cancel
    order_id: int


class AdminCb(CallbackData, prefix="a"):
    """Admin actions on an order."""

    action: str  # done | refunded
    order_id: int


class BankCb(CallbackData, prefix="b"):
    """Bank pick during checkout (id=0 → add new)."""

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
            InlineKeyboardButton(text="🏦 My Bank Cards", callback_data="menu:banks"),
            InlineKeyboardButton(text="📈 Rates", callback_data="menu:rates"),
        ],
        [InlineKeyboardButton(text="🆘 Support", callback_data="menu:support")],
    ])


def services_kb(rates: dict[str, float]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{SERVICES[key]} — 1$/₹{rate:g}",
                              callback_data=f"svc:{key}")]
        for key, rate in rates.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def choose_bank_kb(cards: list[BankCard]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🏦 {c.label}", callback_data=BankCb(card_id=c.id).pack())]
        for c in cards
    ]
    rows.append([InlineKeyboardButton(text="➕ Add new bank", callback_data=BankCb(card_id=0).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def banks_menu_kb(cards: list[BankCard]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🗑 Remove {c.label}", callback_data=BankRmCb(card_id=c.id).pack())]
        for c in cards
    ]
    rows.append([InlineKeyboardButton(text="➕ Add new bank", callback_data="banks:add")])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_placed_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ I've sent the USDT",
                              callback_data=OrderCb(action="sent", order_id=order_id).pack())],
        [InlineKeyboardButton(text="❌ Cancel order",
                              callback_data=OrderCb(action="cancel", order_id=order_id).pack())],
    ])


def order_sent_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Cancel order",
                              callback_data=OrderCb(action="cancel", order_id=order_id).pack())],
    ])


def admin_order_kb(order_id: int, status: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if status in ("submitted", "usdt_sent"):
        rows.append([InlineKeyboardButton(
            text="✅ Done — INR sent",
            callback_data=AdminCb(action="done", order_id=order_id).pack())])
    elif status in ("cancelled", "refund_requested"):
        rows.append([InlineKeyboardButton(
            text="💸 Refund sent",
            callback_data=AdminCb(action="refunded", order_id=order_id).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)
