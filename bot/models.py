import enum
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Naive UTC — SQLite hands naive datetimes back, so we store naive too
    and every comparison stays apples-to-apples."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class OrderStatus(str, enum.Enum):
    AWAITING_DEPOSIT = "awaiting_deposit"  # address shown, watching the chain
    DEPOSIT_RECEIVED = "deposit_received"  # USDT auto-detected, user picking bank
    PENDING_PAYOUT = "pending_payout"      # bank chosen, admin must pay INR
    COMPLETED = "completed"                # admin hit Done — INR credited
    CANCELLED = "cancelled"                # cancelled (refund path if funds came in)
    EXPIRED = "expired"                    # no deposit arrived in time
    REFUND_REQUESTED = "refund_requested"  # user submitted the deposit TXID for a refund
    REFUNDED = "refunded"                  # admin sent the USDT back to the sender
    REFUND_REJECTED = "refund_rejected"    # admin verified: no such deposit (fake TXID)


OPEN_STATUSES = (
    OrderStatus.AWAITING_DEPOSIT,
    OrderStatus.DEPOSIT_RECEIVED,
    OrderStatus.PENDING_PAYOUT,
    OrderStatus.CANCELLED,
    OrderStatus.REFUND_REQUESTED,
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    lang: Mapped[str] = mapped_column(String(4), default="en")
    banned: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BankCard(Base):
    __tablename__ = "bank_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    label: Mapped[str] = mapped_column(String(48))
    details: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    side: Mapped[str] = mapped_column(String(8), default="sell")
    service: Mapped[str] = mapped_column(String(16))
    usd_amount: Mapped[float] = mapped_column(Float)
    rate_inr: Mapped[float] = mapped_column(Float)
    inr_amount: Mapped[float] = mapped_column(Float)
    bank_card_id: Mapped[int | None] = mapped_column(ForeignKey("bank_cards.id"))
    deposit_address: Mapped[str] = mapped_column(String(64))
    refund_address: Mapped[str | None] = mapped_column(String(64))
    txid: Mapped[str | None] = mapped_column(String(80))
    deposit_detected_at: Mapped[datetime | None] = mapped_column(DateTime)
    admin_note: Mapped[str | None] = mapped_column(String(64))
    reminded: Mapped[bool] = mapped_column(default=False)
    refund_txid: Mapped[str | None] = mapped_column(String(80))
    claim_txid: Mapped[str | None] = mapped_column(String(80))  # user-submitted TXID
    # awaiting the admin's manual confirm (auto-detect missed / order expired)
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.AWAITING_DEPOSIT,
                                        index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SeenTx(Base):
    """Every TRC20 deposit the scanner has already processed, so a transfer
    is only ever credited (or alerted on) once."""

    __tablename__ = "seen_txs"

    txid: Mapped[str] = mapped_column(String(80), primary_key=True)
    amount: Mapped[float] = mapped_column(Float)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OrderMsg(Base):
    """Admin-side messages posted for an order — lets admins reply to a card
    to DM the order's user through the bot."""

    __tablename__ = "order_msgs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger)


class Setting(Base):
    """Chat-managed runtime settings: per-service rates, deposit address."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
