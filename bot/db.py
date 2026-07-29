import base64

from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import SERVICES, settings
from .models import Base, Setting, User

engine = create_async_engine(f"sqlite+aiosqlite:///{settings.db_path}")
Session = async_sessionmaker(engine, expire_on_commit=False)

# Columns added after the first release — ensured on an existing DB so an
# in-place upgrade of a live SQLite file never hits "no such column".
_ADDED_COLUMNS = [
    ("users", "lang", "VARCHAR(4) DEFAULT 'en'"),
    ("orders", "txid", "VARCHAR(80)"),
    ("orders", "deposit_detected_at", "DATETIME"),
    ("orders", "admin_note", "VARCHAR(64)"),
    ("orders", "reminded", "BOOLEAN DEFAULT 0"),
    ("orders", "refund_txid", "VARCHAR(80)"),
    ("orders", "claim_txid", "VARCHAR(80)"),
    ("orders", "network", "VARCHAR(8) DEFAULT 'TRC20'"),
    ("orders", "display_address", "VARCHAR(64)"),
    ("orders", "web_token", "VARCHAR(48)"),
]


# Indexes on those late-added columns. create_all() only builds indexes for
# tables it creates, so an upgraded database would otherwise never get them —
# the column appears, the index silently doesn't, and the miss only shows up as
# a slow table scan on the busiest server.
_ADDED_INDEXES = [
    ("ix_orders_web_token", "orders", "web_token"),
]


def _migrate(conn) -> None:
    insp = inspect(conn)
    tables = set(insp.get_table_names())
    for table, col, ddl in _ADDED_COLUMNS:
        if table not in tables:
            continue
        cols = {c["name"] for c in insp.get_columns(table)}
        if col not in cols:
            conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    insp = inspect(conn)   # re-reflect: the loop above just changed the schema,
    for name, table, col in _ADDED_INDEXES:   # and the old inspector is cached
        if table not in tables:
            continue
        if col not in {c["name"] for c in insp.get_columns(table)}:
            continue
        conn.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({col})")


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate)


async def get_or_create_user(session: AsyncSession, tg_id: int,
                             username: str | None, first_name: str | None) -> User:
    user = await session.get(User, tg_id)
    if user is None:
        user = User(id=tg_id, username=username, first_name=first_name)
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            # two first-contact updates raced the insert — the other one won
            await session.rollback()
            user = await session.get(User, tg_id)
        return user
    user.username = username
    user.first_name = first_name
    await session.commit()
    return user


async def get_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.scalar(select(Setting).where(Setting.key == key))
    return row.value if row else None


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    await session.commit()


async def get_rates(session: AsyncSession) -> dict[str, float]:
    """Service key -> INR-per-$ rate, only for services with a live rate set."""
    rates: dict[str, float] = {}
    for key in SERVICES:
        raw = await get_setting(session, f"rate_{key}")
        if raw:
            try:
                rate = float(raw)
            except ValueError:
                continue
            if rate > 0:
                rates[key] = rate
    return rates


async def get_deposit_address(session: AsyncSession) -> str | None:
    return await get_setting(session, "addr_trc20")


async def get_bep20_address(session: AsyncSession) -> str | None:
    return await get_setting(session, "addr_bep20")


async def get_bscscan_key(session: AsyncSession) -> str:
    """A saved row (even blank) is authoritative and can disable BSC via
    /setbsckey off; only fall back to the env key when no row was ever set."""
    row = await session.get(Setting, "bscscan_key")
    if row is not None:
        return (row.value or "").strip()
    return settings.bscscan_key.strip()


async def bep20_active(session: AsyncSession) -> bool:
    """BEP20 is live only when both the deposit address and an API key are set."""
    return bool(await get_bep20_address(session)) and bool(await get_bscscan_key(session))


# ── Per-network deposit QR (admin can upload a custom QR image per chain) ───────
# Stored base64 in the Setting table alongside the exact address it encodes, so a
# QR is only ever shown to a customer when it still matches the live address for
# that network (an address change silently invalidates a stale QR).

MAX_QR_BYTES = 1_000_000      # cap the stored image so the DB stays small (~1 MB)


async def set_network_qr(session: AsyncSession, network: str,
                         img_bytes: bytes, address: str,
                         mime: str = "image/png") -> None:
    net = network.upper()
    await set_setting(session, f"qr_png_b64:{net}", base64.b64encode(img_bytes).decode())
    await set_setting(session, f"qr_png_addr:{net}", address or "")
    await set_setting(session, f"qr_mime:{net}", mime or "image/png")


async def clear_network_qr(session: AsyncSession, network: str) -> None:
    net = network.upper()
    await set_setting(session, f"qr_png_b64:{net}", "")
    await set_setting(session, f"qr_png_addr:{net}", "")
    await set_setting(session, f"qr_mime:{net}", "")


async def get_network_qr_raw(session: AsyncSession,
                             network: str) -> tuple[bytes | None, str, str]:
    """(image_bytes, address_it_encodes, mime) for an uploaded QR, or
    (None, "", "image/png"). Rows stored before mime existed default to PNG."""
    net = network.upper()
    b64 = await get_setting(session, f"qr_png_b64:{net}")
    if not b64:
        return None, "", "image/png"
    addr = await get_setting(session, f"qr_png_addr:{net}") or ""
    mime = await get_setting(session, f"qr_mime:{net}") or "image/png"
    try:
        return base64.b64decode(b64), addr, mime
    except Exception:
        return None, "", "image/png"


async def get_network_qr(session: AsyncSession, network: str,
                         current_address: str | None) -> tuple[bytes, str] | None:
    """(image_bytes, mime) of the uploaded QR ONLY when it still encodes the
    live address for the network — otherwise None, so the caller falls back to
    an auto-generated QR that always matches the address the customer pays."""
    img, addr, mime = await get_network_qr_raw(session, network)
    if img and current_address and addr == current_address:
        return img, mime
    return None


DEFAULT_DEPOSIT_TTL_MIN = 15   # product default; the stale P2P_DEPOSIT_TTL_MIN env
                               # is intentionally NOT consulted here — the panel is
                               # the single source of truth for the live window.


async def get_deposit_ttl(session: AsyncSession) -> int:
    """Minutes a deposit quote stays live before it expires. The admin panel
    (Settings → Deposit window) is the source of truth; when it's unset we use a
    hard 15-min default and ignore the env var, so an old P2P_DEPOSIT_TTL_MIN=60
    left in .env can never quietly make quotes last an hour. Clamped to a sane
    floor so the reminder window never inverts."""
    raw = await get_setting(session, "deposit_ttl_min")
    try:
        if raw and int(raw) > 0:
            return max(2, int(raw))
    except ValueError:
        pass
    return DEFAULT_DEPOSIT_TTL_MIN


async def get_service_limits(session: AsyncSession, service: str) -> tuple[float, float]:
    """Per-service min/max USD, panel-editable; falls back to the global env
    bounds when unset."""
    lo, hi = settings.min_usd, settings.max_usd
    raw_lo = await get_setting(session, f"limit_min_{service}")
    raw_hi = await get_setting(session, f"limit_max_{service}")
    try:
        if raw_lo and float(raw_lo) > 0:
            lo = float(raw_lo)
    except ValueError:
        pass
    try:
        if raw_hi and float(raw_hi) > 0:
            hi = float(raw_hi)
    except ValueError:
        pass
    return lo, hi


async def get_desk_open(session: AsyncSession) -> bool:
    """Master on/off switch for taking new sell orders (default open)."""
    return (await get_setting(session, "desk_open")) != "0"


async def desk_state(session: AsyncSession) -> tuple[bool, str]:
    """(open?, reason). Open needs the switch on + an address + at least one rate."""
    if not await get_desk_open(session):
        return False, "manually closed"
    if not await get_deposit_address(session):
        return False, "no deposit address set"
    if not await get_rates(session):
        return False, "no service rate set"
    return True, "open"


async def get_support(session: AsyncSession) -> str:
    """Support contact(s) — chat-managed via /setsupport, env fallback."""
    return (await get_setting(session, "support")) or settings.support_handle


async def get_whatsapp(session: AsyncSession) -> str:
    """WhatsApp support number — chat-managed via /setwhatsapp, env fallback.
    A saved row (even blank, via /setwhatsapp off) is authoritative."""
    row = await session.get(Setting, "support_whatsapp")
    if row is not None:
        return (row.value or "").strip()
    return settings.support_whatsapp.strip()


async def get_support_email(session: AsyncSession) -> str:
    """Support email shown on the website (panel-managed). Falls back to the
    business email so /about and support stay consistent. A saved row is
    authoritative (blank turns it off)."""
    row = await session.get(Setting, "support_email")
    if row is not None:
        return (row.value or "").strip()
    return settings.biz_email.strip()


async def get_lang(session: AsyncSession, user_id: int) -> str:
    user = await session.get(User, user_id)
    return user.lang if user and user.lang else "en"


# ── Runtime config: editable from the web panel, DB-backed with env bootstrap ──

async def get_admin_ids(session: AsyncSession) -> set[int]:
    raw = await get_setting(session, "admin_ids")
    source = raw if raw is not None else settings.admin_ids
    return {int(x) for x in source.replace(",", " ").split() if x.strip().isdigit()}


async def get_admin_chat_id(session: AsyncSession) -> int | None:
    raw = await get_setting(session, "admin_chat_id")
    if raw is None:
        return settings.admin_chat_id
    raw = raw.strip()
    return int(raw) if raw.lstrip("-").isdigit() else None


async def get_admin_targets(session: AsyncSession) -> list[int]:
    chat = await get_admin_chat_id(session)
    return [chat] if chat else sorted(await get_admin_ids(session))


async def get_bot_token(session: AsyncSession) -> str:
    raw = await get_setting(session, "bot_token")
    return (raw or "").strip() or settings.bot_token


async def is_admin(user_id: int) -> bool:
    async with Session() as session:
        return user_id in await get_admin_ids(session)
