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
    # optional email for order updates (/email in the bot) — only ever used
    # after the OTP check sets email_verified, so fake addresses die at the door
    email: Mapped[str] = mapped_column(String(190), default="")
    email_verified: Mapped[bool] = mapped_column(default=False)
    # one-time "get receipts by email" nudge after their first order
    email_prompted: Mapped[bool] = mapped_column(default=False)
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
    # The chain the customer chose to pay on ("TRC20" / "BEP20"). deposit_address
    # always stays the TRC20 desk address (the amount-based scanner matches there),
    # so these two tell every LATER screen which address/QR to re-show. display_address
    # pins the EXACT address the customer first saw (so rotating the BEP20 desk address
    # later can't make a reminder show a different address than the first message).
    network: Mapped[str] = mapped_column(String(8), default="TRC20")
    display_address: Mapped[str | None] = mapped_column(String(64))
    # Unguessable token for the website order page (/o/<token>) — web orders only.
    # Web customers have negative user ids (no Telegram account behind them).
    web_token: Mapped[str | None] = mapped_column(String(48), index=True)
    refund_address: Mapped[str | None] = mapped_column(String(64))
    txid: Mapped[str | None] = mapped_column(String(80))
    deposit_detected_at: Mapped[datetime | None] = mapped_column(DateTime)
    admin_note: Mapped[str | None] = mapped_column(String(64))
    reminded: Mapped[bool] = mapped_column(default=False)
    refund_txid: Mapped[str | None] = mapped_column(String(80))
    claim_txid: Mapped[str | None] = mapped_column(String(80))  # user-submitted TXID
    # the submitted TXID could NOT be seen on-chain (or the API was down) — it
    # went to the admin as an UNVERIFIED claim for manual checking. Exactly one
    # such submission is allowed per order; a verified TXID may replace it.
    claim_unverified: Mapped[bool] = mapped_column(default=False)
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


class Ticket(Base):
    """Customer support tickets — filed from the website (deposit sent but not
    credited, payout issues, anything else) and worked from the admin panel.
    Web customers have no Telegram chat, so `contact` is how the desk reaches
    them back."""

    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    category: Mapped[str] = mapped_column(String(24))       # deposit / payout / other
    txid: Mapped[str | None] = mapped_column(String(80))
    contact: Mapped[str] = mapped_column(String(120), default="")
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Account(Base):
    """Website customer accounts — the signup gate in front of the sell flow.
    Each account owns a stable negative user id, -(2^48 + id), so its orders,
    bank cards and tickets follow the login across devices while never
    colliding with anonymous browser uids (magnitude < 2^47) or Telegram ids
    (positive). Google users have google_sub set and no password; manual
    users have pw_salt/pw_hash (PBKDF2) and usually a phone."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(190), unique=True, index=True)
    # Google sign-ins are verified by Google; manual signups verify via a
    # 6-digit OTP. Order/marketing email only ever goes to verified addresses.
    email_verified: Mapped[bool] = mapped_column(default=False)
    name: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(20), default="")
    provider: Mapped[str] = mapped_column(String(10), default="email")  # email / google
    google_sub: Mapped[str | None] = mapped_column(String(64), index=True)
    pw_salt: Mapped[str] = mapped_column(String(32), default="")
    pw_hash: Mapped[str] = mapped_column(String(64), default="")
    stock: Mapped[str] = mapped_column(String(8), default="")   # daily USDT stock tier
    # session version — baked into the login cookie's signature; bumping it
    # (password reset, Google takeover) instantly signs out EVERY device
    sess_ver: Mapped[int] = mapped_column(Integer, default=0)
    # one-shot re-engagement nudge: emailed once ~12h after signup if the
    # account still hasn't completed an order (0 = not yet sent, 1 = sent)
    nudged: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    """Chat-managed runtime settings: per-service rates, deposit address."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Unsubscribe(Base):
    """Emails that opted out of bulk mail (via the one-click List-Unsubscribe
    link). The bulk email sender skips every address stored here — so an
    unsubscribe is honoured for good, keeping the sending domain's reputation
    clean and the desk compliant."""

    __tablename__ = "email_unsubscribes"

    email: Mapped[str] = mapped_column(String(190), primary_key=True)  # lower-cased
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PhoneHistory(Base):
    """Every mobile number a website account has moved AWAY from. When a
    customer edits their number on the account page the old one isn't erased —
    it's kept here so the desk never loses a contact it once had. The panel
    shows these and the phone-number CSV export includes them, so a bulk SMS /
    WhatsApp list still reaches a customer who changed their number."""

    __tablename__ = "phone_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True)
    old_phone: Mapped[str] = mapped_column(String(20))     # the number they left
    new_phone: Mapped[str] = mapped_column(String(20), default="")  # what replaced it
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
