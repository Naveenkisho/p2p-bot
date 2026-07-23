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


async def get_support(session: AsyncSession) -> str:
    """Support contact(s) — chat-managed via /setsupport, env fallback."""
    return (await get_setting(session, "support")) or settings.support_handle


async def get_lang(session: AsyncSession, user_id: int) -> str:
    user = await session.get(User, user_id)
    return user.lang if user and user.lang else "en"
