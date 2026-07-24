"""TRON auto-scan.

Polls TronGrid for confirmed USDT (TRC20) *transfers* into the desk's deposit
address(es) and credits the matching order. Design points that keep it safe
for real money:

- Only ``type == "Transfer"`` events count — approvals (zero-value allowance
  grants) are ignored.
- Each address carries an activation watermark (``addr_since:<addr>``, set by
  /setaddress). Transfers with a block time at/なbefore the watermark are the
  address's pre-existing history and are silently marked seen; only transfers
  strictly after it can credit an order. This replaces any fragile first-run
  flag and
  makes address rotation safe.
- A transfer is matched only to an AWAITING_DEPOSIT order that was quoted the
  *same* address and the same amount. If exactly one such order exists it is
  credited atomically (status flip + seen-tx row in one commit). If several
  orders share that amount the deposit is held for manual assignment
  (``/received <id> <txid>``) rather than auto-crediting the wrong user.
- Deposits to a rotated-away address are still scanned as long as an order is
  awaiting on it.
- The loop can never exit: every error is caught and retried next tick.
"""

import asyncio
import logging

import aiohttp
from aiogram import Bot
from sqlalchemy import select, update

from . import texts
from .config import settings
from .db import Session, get_deposit_address, get_setting, set_setting
from .flow import notify_deposit_received
from .helpers import notify_admins
from .models import Order, OrderStatus, SeenTx, utcnow

log = logging.getLogger(__name__)

AMOUNT_TOLERANCE = 0.005  # USDT — exact match with float slack


def _ms(dt) -> int:
    # naive UTC datetime (see models.utcnow) → epoch ms
    return int(dt.replace(microsecond=0).timestamp() * 1000)


async def address_watermark(session, address: str) -> int:
    """Epoch-ms cutoff for an address: transfers at/before it are history.

    Uses the /setaddress activation time; falls back to the earliest awaiting
    order on that address, else 'now' (treat all current history as old)."""
    raw = await get_setting(session, f"addr_since:{address}")
    if raw and raw.isdigit():
        return int(raw)
    earliest = await session.scalar(
        select(Order.created_at).where(
            Order.deposit_address == address,
            Order.status == OrderStatus.AWAITING_DEPOSIT.value,
        ).order_by(Order.id).limit(1))
    return _ms(earliest) if earliest else _ms(utcnow())


async def addresses_to_scan(session) -> list[str]:
    """Current deposit address plus any address still carrying an awaiting
    order (so deposits to a rotated-away address are still detected)."""
    addrs: list[str] = []
    current = await get_deposit_address(session)
    if current:
        addrs.append(current)
    rows = (await session.scalars(
        select(Order.deposit_address).where(
            Order.status == OrderStatus.AWAITING_DEPOSIT.value).distinct())).all()
    for a in rows:
        if a and a not in addrs:
            addrs.append(a)
    return addrs


def transfer_amount(tx: dict) -> float | None:
    if (tx.get("type") or "Transfer") != "Transfer":
        return None
    token = tx.get("token_info") or {}
    if token.get("address") and token["address"] != settings.usdt_contract:
        return None
    try:
        decimals = int(token.get("decimals", 6))
        return int(tx.get("value", "0")) / (10 ** decimals)
    except (TypeError, ValueError):
        return None


async def fetch_transfers(http: aiohttp.ClientSession, address: str,
                          min_ts: int) -> list[dict]:
    """All confirmed inbound USDT transfers to `address` newer than min_ts,
    oldest-first, following TronGrid pagination up to a page cap."""
    url = f"{settings.trongrid_url}/v1/accounts/{address}/transactions/trc20"
    params = {
        "only_to": "true",
        "only_confirmed": "true",
        "limit": str(settings.scan_page_limit),
        "contract_address": settings.usdt_contract,
        "order_by": "block_timestamp,asc",
        "min_timestamp": str(min_ts + 1),
    }
    headers = {"TRON-PRO-API-KEY": settings.trongrid_key} if settings.trongrid_key else {}
    out: list[dict] = []
    for _ in range(settings.scan_max_pages):
        async with http.get(url, params=params, headers=headers) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        data = payload.get("data") or []
        out.extend(data)
        fingerprint = (payload.get("meta") or {}).get("fingerprint")
        if not fingerprint or len(data) < settings.scan_page_limit:
            break
        params["fingerprint"] = fingerprint
    return out


async def _credit_or_hold(bot: Bot, tx: dict, address: str) -> None:
    """Match one confirmed transfer to an awaiting order on this address."""
    txid = tx.get("transaction_id")
    amount = transfer_amount(tx)
    if not txid or amount is None or amount <= 0:
        return
    if (tx.get("to") or "") != address:
        return

    async with Session() as session:
        if await session.get(SeenTx, txid) is not None:
            return
        candidates = (await session.scalars(
            select(Order).where(
                Order.status == OrderStatus.AWAITING_DEPOSIT.value,
                Order.deposit_address == address,
                Order.usd_amount >= amount - AMOUNT_TOLERANCE,
                Order.usd_amount <= amount + AMOUNT_TOLERANCE,
            ).order_by(Order.id))).all()

        if len(candidates) == 1:
            order = candidates[0]
            res = await session.execute(
                update(Order)
                .where(Order.id == order.id,
                       Order.status == OrderStatus.AWAITING_DEPOSIT.value)
                .values(status=OrderStatus.DEPOSIT_RECEIVED.value,
                        txid=txid, deposit_detected_at=utcnow()))
            if res.rowcount == 0:
                # the order was cancelled/expired between SELECT and UPDATE —
                # leave the tx unseen so the next tick re-evaluates it
                await session.rollback()
                return
            session.add(SeenTx(txid=txid, amount=amount, order_id=order.id))
            await session.commit()
            await notify_deposit_received(bot, order.id)
            return

        # 0 or 2+ candidates: record the tx so we don't re-alert, then tell
        # the admins. Ambiguous amounts are held for manual assignment.
        session.add(SeenTx(txid=txid, amount=amount, order_id=None))
        await session.commit()

    if not candidates:
        # show the open orders so the admin has context (typo? overpay? late payer
        # on an already-expired order? no order at all?)
        async with Session() as session:
            opens = (await session.scalars(
                select(Order).where(
                    Order.status == OrderStatus.AWAITING_DEPOSIT.value)
                .order_by(Order.id))).all()
        ctx = "\n".join(f"• {texts.tag(o.id)} expects <b>{texts.usd_str(o.usd_amount)}$</b>"
                        for o in opens[:8]) or "• (no orders waiting)"
        await notify_admins(
            bot,
            f"⚠️ <b>Unmatched deposit: {texts.usd_str(amount)} USDT</b> "
            f"(tx <code>{txid}</code>)\n"
            f"No open order expects exactly {texts.usd_str(amount)}$ "
            f"(a sender's platform may have deducted a fee).\n\n"
            f"<b>Open orders:</b>\n{ctx}\n\n"
            f"If it's for one of them, credit the <b>actual {texts.usd_str(amount)}</b> "
            f"received (not the ordered amount):\n"
            f"<code>/received &lt;id&gt; {txid}</code>\n"
            f"Otherwise refund the sender.")
    else:
        ids = ", ".join(texts.tag(o.id) for o in candidates)
        await notify_admins(bot,
                            f"⚠️ <b>{texts.usd_str(amount)} USDT</b> deposit "
                            f"(tx <code>{txid}</code>) "
                            f"matches {len(candidates)} awaiting orders: {ids}.\n"
                            f"Confirm the correct one manually: "
                            f"/received &lt;id&gt; {txid}")


async def expire_stale_orders(bot: Bot) -> None:
    from datetime import timedelta

    from .helpers import try_transition, update_order_cards
    from .keyboards import admin_order_kb
    from .models import BankCard, User

    cutoff = utcnow() - timedelta(minutes=settings.deposit_ttl_min)
    expired: list[tuple[int, int, str]] = []
    async with Session() as session:
        stale = (await session.scalars(
            select(Order).where(Order.status == OrderStatus.AWAITING_DEPOSIT.value,
                                Order.created_at < cutoff))).all()
        for order in stale:
            updated = await try_transition(session, order.id,
                                           (OrderStatus.AWAITING_DEPOSIT,),
                                           OrderStatus.EXPIRED)
            if updated is not None:
                user = await session.get(User, order.user_id)
                card = await session.get(BankCard, order.bank_card_id) \
                    if order.bank_card_id else None
                await update_order_cards(bot, session, updated, user, card, None)
                expired.append((order.user_id,
                                user.lang if user and user.lang else "en", order.id))
    if expired:
        from .db import get_support
        from .helpers import notify_user
        from .keyboards import expired_kb
        async with Session() as session:
            support = await get_support(session)
        for user_id, lang, order_id in expired:
            await notify_user(bot, user_id,
                              texts.order_expired(order_id, support, lang),
                              reply_markup=expired_kb(order_id))


async def _bootstrap_addresses(session, http: aiohttp.ClientSession) -> None:
    """One-time: mark an address's existing history seen so old transfers are
    never credited. Idempotent — a 'bootstrapped:<addr>' flag is durable."""
    for address in await addresses_to_scan(session):
        if await get_setting(session, f"bootstrapped:{address}"):
            continue
        watermark = await address_watermark(session, address)
        try:
            history = await fetch_transfers(http, address, 0)
        except Exception:
            log.exception("bootstrap fetch failed for %s", address)
            continue
        for tx in history:
            txid = tx.get("transaction_id")
            ts = int(tx.get("block_timestamp", 0) or 0)
            if txid and ts <= watermark and await session.get(SeenTx, txid) is None:
                session.add(SeenTx(txid=txid,
                                   amount=transfer_amount(tx) or 0.0, order_id=None))
        await set_setting(session, f"bootstrapped:{address}", "1")
    await session.commit()


async def remind_pending_orders(bot: Bot) -> None:
    """Nudge users who created an order but haven't deposited after remind_min
    (once per order), before it eventually expires."""
    from datetime import timedelta

    from .helpers import notify_user
    from .keyboards import deposit_kb
    from .models import User

    now = utcnow()
    due = now - timedelta(minutes=settings.remind_min)
    not_expired = now - timedelta(minutes=settings.deposit_ttl_min)
    pending: list[tuple[int, int, float, str, str]] = []
    async with Session() as session:
        rows = (await session.scalars(
            select(Order).where(
                Order.status == OrderStatus.AWAITING_DEPOSIT.value,
                Order.reminded.is_(False),
                Order.created_at < due,
                Order.created_at > not_expired))).all()
        for o in rows:
            o.reminded = True
            user = await session.get(User, o.user_id)
            lang = user.lang if user and user.lang else "en"
            pending.append((o.user_id, o.id, o.usd_amount, o.deposit_address, lang))
        await session.commit()
    for uid, oid, usd, addr, lang in pending:
        await notify_user(bot, uid, texts.deposit_reminder(oid, usd, addr, lang),
                          reply_markup=deposit_kb(oid))


async def scan_once(bot: Bot, http: aiohttp.ClientSession) -> None:
    async with Session() as session:
        await _bootstrap_addresses(session, http)
        plan = {a: await address_watermark(session, a)
                for a in await addresses_to_scan(session)}
    for address, watermark in plan.items():
        transfers = await fetch_transfers(http, address, watermark)
        for tx in transfers:
            if int(tx.get("block_timestamp", 0) or 0) <= watermark:
                continue
            await _credit_or_hold(bot, tx, address)
    await remind_pending_orders(bot)
    await expire_stale_orders(bot)


_checking: set[int] = set()
_check_tasks: set = set()


async def check_order_now(bot: Bot, order_id: int) -> str | None:
    """On-demand scan of a single order's deposit address (triggered when the
    user taps 'Check status'). Returns the order's status afterwards."""
    async with Session() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return None
        address = order.deposit_address
        watermark = await address_watermark(session, address)
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as http:
            transfers = await fetch_transfers(http, address, watermark)
        for tx in transfers:
            if int(tx.get("block_timestamp", 0) or 0) <= watermark:
                continue
            await _credit_or_hold(bot, tx, address)
    except Exception:
        log.exception("on-demand check failed for order %s", order_id)
    async with Session() as session:
        o = await session.get(Order, order_id)
        return o.status if o else None


async def lookup_claim_tx(txid: str, address: str, since_ms: int) -> dict:
    """Look up a user-submitted TXID on-chain to help the admin verify a
    late/missed payment: is it a confirmed USDT transfer TO `address`, for how
    much, and when? Returns {found, error, amount, to, to_ok, timestamp}."""
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as http:
            transfers = await fetch_transfers(http, address, max(0, since_ms - 3_600_000))
    except Exception:
        log.exception("claim lookup failed for %s", txid)
        return {"found": False, "error": True}
    for tx in transfers:
        if (tx.get("transaction_id") or "") != txid:
            continue
        return {"found": True, "error": False,
                "amount": transfer_amount(tx),
                "to": tx.get("to") or "",
                "to_ok": (tx.get("to") or "") == address,
                "timestamp": int(tx.get("block_timestamp", 0) or 0)}
    return {"found": False, "error": False}


CHECK_ROUNDS = 5        # scans spread across the wait window
CHECK_INTERVAL = 15     # seconds between scans (≈60s total)


def launch_order_check(bot: Bot, order_id: int) -> bool:
    """DM the user 'checking, wait ~60s', re-scan the address across a ~60s
    window (so a freshly-sent transfer has time to confirm), then DM the
    result. Returns False if a check for this order is already running."""
    if order_id in _checking:
        return False
    _checking.add(order_id)

    async def _run():
        from .db import get_support
        from .helpers import notify_user
        from .keyboards import not_detected_kb
        from .models import User
        try:
            async with Session() as session:
                o = await session.get(Order, order_id)
                if o is None:
                    return
                user_id = o.user_id
                user = await session.get(User, user_id)
                lang = user.lang if user and user.lang else "en"
            await notify_user(bot, user_id, texts.checking_wait(lang))

            for i in range(CHECK_ROUNDS):
                status = await check_order_now(bot, order_id)
                if status != OrderStatus.AWAITING_DEPOSIT.value:
                    return  # verified/closed — the verified DM was already sent
                if i < CHECK_ROUNDS - 1:
                    await asyncio.sleep(CHECK_INTERVAL)

            async with Session() as session:
                o = await session.get(Order, order_id)
                support = await get_support(session)
            if o and o.status == OrderStatus.AWAITING_DEPOSIT.value:
                await notify_user(bot, user_id,
                                  texts.payment_not_detected(o.id, support, lang),
                                  reply_markup=not_detected_kb(o.id))
        finally:
            _checking.discard(order_id)

    task = asyncio.create_task(_run())
    _check_tasks.add(task)
    task.add_done_callback(_check_tasks.discard)
    return True


async def scan_loop(bot: Bot) -> None:
    """Never exits: any error (network, DB, TronGrid) is logged and retried."""
    timeout = aiohttp.ClientTimeout(total=15)
    while True:
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as http:
                while True:
                    try:
                        await scan_once(bot, http)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("tron scan tick failed; retrying")
                    await asyncio.sleep(settings.scan_interval_sec)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("tron scan session died; recreating")
            await asyncio.sleep(settings.scan_interval_sec)
