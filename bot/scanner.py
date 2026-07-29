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
import time

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
    # naive UTC datetime (see models.utcnow) → epoch ms. Pin it to UTC before
    # converting so .timestamp() never reinterprets it in the server's local zone
    # (that would skew every on-chain time comparison on a non-UTC host, e.g. IST).
    from datetime import timezone
    return int(dt.replace(microsecond=0, tzinfo=timezone.utc).timestamp() * 1000)


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
                          min_ts: int, confirmed: bool = True) -> list[dict]:
    """Inbound USDT transfers to `address` newer than min_ts, oldest-first,
    following TronGrid pagination up to a page cap. confirmed=True returns
    only solidified transfers (safe to credit); confirmed=False returns the
    just-broadcast ones wallets show instantly — used ONLY to tell the
    customer "we see it", never to credit."""
    url = f"{settings.trongrid_url}/v1/accounts/{address}/transactions/trc20"
    params = {
        "only_to": "true",
        "only_confirmed" if confirmed else "only_unconfirmed": "true",
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


async def _credit_amount(bot: Bot, txid: str, amount: float, address: str,
                         chain: str) -> None:
    """Credit one confirmed USDT deposit (TRC20 or BEP20) to the awaiting order
    whose unique amount tag it matches. The AMOUNT alone identifies the order —
    the chain/address is just where the money landed — so a deposit on either
    chain lands on the right order with no cross-chain conflict (this is safe
    precisely because unique-cents gives every open order a distinct amount)."""
    if not txid or amount is None or amount <= 0:
        return
    async with Session() as session:
        if await session.get(SeenTx, txid) is not None:
            return
        candidates = (await session.scalars(
            select(Order).where(
                Order.status == OrderStatus.AWAITING_DEPOSIT.value,
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
            deposit_seen.pop(order.id, None)   # sighting fulfilled — confirmed
            await notify_deposit_received(bot, order.id)
            return

        # 0 or 2+ candidates: record the tx so we don't re-alert, then tell
        # the admins. Ambiguous amounts are held for manual assignment.
        session.add(SeenTx(txid=txid, amount=amount, order_id=None))
        await session.commit()

    tag = f"({chain}, tx <code>{txid}</code>)"
    if not candidates:
        async with Session() as session:
            opens = (await session.scalars(
                select(Order).where(
                    Order.status == OrderStatus.AWAITING_DEPOSIT.value)
                .order_by(Order.id))).all()
        ctx = "\n".join(f"• {texts.tag(o.id)} expects <b>{texts.usd_str(o.usd_amount)}$</b>"
                        for o in opens[:8]) or "• (no orders waiting)"
        await notify_admins(
            bot,
            f"⚠️ <b>Unmatched deposit: {texts.usd_str(amount)} USDT</b> {tag}\n"
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
                            f"⚠️ <b>{texts.usd_str(amount)} USDT</b> deposit {tag} "
                            f"matches {len(candidates)} awaiting orders: {ids}.\n"
                            f"Confirm the correct one manually: "
                            f"/received &lt;id&gt; {txid}")


async def _credit_or_hold(bot: Bot, tx: dict, address: str) -> None:
    """TRON adapter: pull the fields off a TronGrid transfer and credit it."""
    txid = tx.get("transaction_id")
    amount = transfer_amount(tx)
    if not txid or amount is None or amount <= 0:
        return
    if (tx.get("to") or "") != address:
        return
    await _credit_amount(bot, txid, amount, address, "TRC20")


# ── instant sighting of unconfirmed deposits ─────────────────────────────────
# TRON "confirmed" means ~19 blocks (about a minute) — wallets feel instant
# because they display the unconfirmed transaction straight away. This gives
# our customers the same moment: the second the transfer appears on-chain the
# order is marked "deposit detected — confirming" (order page banner + one DM
# for Telegram users). CREDIT still happens only from the confirmed sweep —
# an unconfirmed sighting never moves an order or queues a payout.

# order_id -> (txid, first_seen_monotonic). Kept for an order's whole life so
# a single sighting is announced exactly once: the entry is dropped when the
# deposit confirms (credit) or the order expires — NOT on a short timer, which
# would let a slow-to-solidify transfer re-announce itself. The TTL below is
# only a memory backstop, set above the largest deposit window the panel allows
# (24h) so it never fires while an order can still be awaiting.
deposit_seen: dict[int, tuple[str, float]] = {}
_SEEN_TTL = 26 * 3600
_SEEN_CAP = 5000


def _prune_seen() -> None:
    now = time.monotonic()
    for oid in [oid for oid, (_, t0) in deposit_seen.items()
                if now - t0 > _SEEN_TTL]:
        deposit_seen.pop(oid, None)
    if len(deposit_seen) > _SEEN_CAP:       # hard cap: drop the oldest entries
        for oid, _ in sorted(deposit_seen.items(), key=lambda kv: kv[1][1]
                             )[:len(deposit_seen) - _SEEN_CAP]:
            deposit_seen.pop(oid, None)


async def _note_unconfirmed(bot: Bot, tx: dict, address: str) -> None:
    """Match one unconfirmed transfer to its awaiting order by unique amount
    and announce the sighting. No state transition, no SeenTx row — if the
    transfer never solidifies, nothing was promised or credited."""
    txid = tx.get("transaction_id")
    amount = transfer_amount(tx)
    if not txid or amount is None or amount <= 0:
        return
    if (tx.get("to") or "") != address:
        return
    async with Session() as session:
        if await session.get(SeenTx, txid) is not None:
            return                     # confirmed sweep already handled it
        instant = (await get_setting(session, "instant_credit")) == "1"
        candidates = (await session.scalars(
            select(Order).where(
                Order.status == OrderStatus.AWAITING_DEPOSIT.value,
                Order.usd_amount >= amount - AMOUNT_TOLERANCE,
                Order.usd_amount <= amount + AMOUNT_TOLERANCE,
            ).order_by(Order.id))).all()
    if len(candidates) != 1:           # ambiguity is the confirmed path's job
        return
    order = candidates[0]
    # instant-credit mode (opt-in): the deposit is already mined into a block
    # (TronGrid "unconfirmed" = mined but not yet solidified), and unique-cents
    # makes the amount→order match exact, so credit it now instead of waiting
    # ~1 min for solidification. _credit_amount writes a SeenTx row, so when the
    # same tx later lands in the confirmed sweep it is deduped, never double-paid.
    if instant:
        deposit_seen.pop(order.id, None)
        await _credit_amount(bot, txid, amount, address, "TRC20")
        return
    if order.id in deposit_seen:
        return
    deposit_seen[order.id] = (txid, time.monotonic())
    log.info("deposit sighted unconfirmed for order %s (%.2f USDT)",
             order.id, amount)
    try:
        from .helpers import notify_user
        await notify_user(
            bot, order.user_id,
            f"⚡ <b>Deposit detected on-chain!</b>\n"
            f"{texts.usd_str(amount)} USDT is confirming now — usually under "
            f"a minute. It will be credited to {texts.tag(order.id)} "
            f"automatically the moment it confirms.")
    except Exception:
        log.exception("deposit-seen DM failed for order %s", order.id)


# ── BEP20 / BSC (second chain) ─────────────────────────────────────────────────

def bsc_transfer_amount(tx: dict) -> float | None:
    if (tx.get("contractAddress") or "").lower() != settings.bep20_usdt_contract.lower():
        return None
    try:
        return int(tx.get("value", "0")) / (10 ** int(tx.get("tokenDecimal", 18)))
    except (TypeError, ValueError):
        return None


async def fetch_bsc_transfers(http: aiohttp.ClientSession, address: str,
                              min_ts: int, key: str) -> list[dict]:
    """Inbound USDT (BEP20) transfers to `address` with a block time after
    `min_ts` (unix seconds), via BscScan/Etherscan-v2, oldest-first across pages
    (so a burst larger than one page is never dropped). Never lets the key-bearing
    request URL reach the logs."""
    base = {
        "chainid": str(settings.bsc_chainid), "module": "account", "action": "tokentx",
        "contractaddress": settings.bep20_usdt_contract, "address": address,
        "sort": "desc", "offset": str(settings.scan_page_limit), "apikey": key,
    }
    out: list[dict] = []
    for page in range(1, settings.scan_max_pages + 1):
        params = {**base, "page": str(page)}
        try:
            async with http.get(settings.bscscan_url, params=params) as resp:
                resp.raise_for_status()
                payload = await resp.json()
        except Exception as e:                     # never log the URL (carries the apikey)
            log.warning("BSC fetch failed: %s", type(e).__name__)
            break
        if str(payload.get("status")) != "1":
            break   # "0" = no (more) txns / rate-limited — nothing to credit this tick
        rows = payload.get("result") or []
        reached_old = False
        for tx in rows:                            # newest-first
            try:
                ts = int(tx.get("timeStamp", 0) or 0)
            except (TypeError, ValueError):
                continue
            if ts <= min_ts:
                reached_old = True                 # everything past here is older too
                break
            if (tx.get("to") or "").lower() == address.lower():
                out.append(tx)
        if reached_old or len(rows) < settings.scan_page_limit:
            break
    return out


async def _credit_bsc(bot: Bot, tx: dict, address: str) -> None:
    txid = (tx.get("hash") or "").lower()      # store 0x-hashes lower-cased & consistent
    amount = bsc_transfer_amount(tx)
    if not txid or amount is None or amount <= 0:
        return
    if (tx.get("to") or "").lower() != address.lower():
        return
    await _credit_amount(bot, txid, amount, address, "BEP20")


async def bsc_watermark(session, address: str) -> int:
    """Unix-seconds cutoff for the BEP20 address; transfers at/before it are
    pre-activation history and never credited."""
    raw = await get_setting(session, f"bsc_since:{address}")
    return int(raw) if raw and raw.isdigit() else int(utcnow().timestamp())


async def scan_bsc_once(bot: Bot, http: aiohttp.ClientSession) -> None:
    from .db import bep20_active, get_bep20_address, get_bscscan_key
    async with Session() as session:
        if not await bep20_active(session):
            return
        address = await get_bep20_address(session)
        key = await get_bscscan_key(session)
        watermark = await bsc_watermark(session, address)
    try:
        transfers = await fetch_bsc_transfers(http, address, watermark, key)
    except Exception as e:
        log.warning("BSC scan fetch failed: %s", type(e).__name__)   # no key in logs
        return
    newest = watermark
    for tx in transfers:
        await _credit_bsc(bot, tx, address)
        try:
            newest = max(newest, int(tx.get("timeStamp", 0) or 0))
        except (TypeError, ValueError):
            pass
    if newest > watermark:
        # advance the watermark (2-min look-back for same-second stragglers) so the
        # next tick fetches a small window — page 1 then covers any burst.
        async with Session() as session:
            await set_setting(session, f"bsc_since:{address}", str(newest - 120))


async def lookup_bsc_tx(txid: str, since_ms: int) -> dict:
    """Look up a BEP20 TXID on-chain (BscScan) to verify a claim / reconcile the
    actual amount. Returns the same shape as lookup_claim_tx (timestamp in ms)."""
    from .db import bep20_active, get_bep20_address, get_bscscan_key
    async with Session() as session:
        if not await bep20_active(session):
            return {"found": False, "error": False}
        address = await get_bep20_address(session)
        key = await get_bscscan_key(session)
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as http:
            transfers = await fetch_bsc_transfers(http, address, 0, key)
    except Exception as e:
        log.warning("BSC claim lookup failed: %s", type(e).__name__)   # no key in logs
        return {"found": False, "error": True}
    for tx in transfers:
        if (tx.get("hash") or "").lower() == (txid or "").lower():
            return {"found": True, "error": False,
                    "amount": bsc_transfer_amount(tx),
                    "to": tx.get("to") or "",
                    "to_ok": (tx.get("to") or "").lower() == address.lower(),
                    "timestamp": int(tx.get("timeStamp", 0) or 0) * 1000}
    return {"found": False, "error": False}


async def expire_stale_orders(bot: Bot) -> None:
    from datetime import timedelta

    from .db import get_deposit_ttl
    from .helpers import try_transition, update_order_cards
    from .keyboards import admin_order_kb
    from .models import BankCard, User

    async with Session() as session:
        ttl_min = await get_deposit_ttl(session)
    cutoff = utcnow() - timedelta(minutes=ttl_min)
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
                deposit_seen.pop(order.id, None)   # sighting is moot once expired
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
                              texts.order_expired(order_id, support, lang, ttl_min=ttl_min),
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

    from .helpers import notify_user, order_display_address
    from .keyboards import deposit_kb
    from .models import User

    from .db import get_deposit_ttl, get_support

    now = utcnow()
    async with Session() as session:
        ttl_min = await get_deposit_ttl(session)
    due = now - timedelta(minutes=settings.remind_min)
    not_expired = now - timedelta(minutes=ttl_min)
    pending: list[tuple[int, int, float, str, str, str]] = []
    async with Session() as session:
        support = await get_support(session)
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
            # Re-show the address for the network the customer PICKED (never the raw
            # TRC20 deposit_address) so a BEP20 order isn't reminded with a TRON one.
            show_addr, net_label, _ = order_display_address(o)
            pending.append((o.user_id, o.id, o.usd_amount, show_addr, net_label, lang))
        await session.commit()
    for uid, oid, usd, addr, net_label, lang in pending:
        await notify_user(bot, uid,
                          texts.deposit_reminder(oid, usd, addr, net_label, lang,
                                                 support, ttl_min=ttl_min),
                          reply_markup=deposit_kb(oid))


async def scan_once(bot: Bot, http: aiohttp.ClientSession) -> None:
    async with Session() as session:
        await _bootstrap_addresses(session, http)
        plan = {a: await address_watermark(session, a)
                for a in await addresses_to_scan(session)}
        # addresses that actually have an awaiting order right now — the ONLY
        # ones worth the extra unconfirmed call (the desk address with nothing
        # pending would just burn quota)
        awaiting = set((await session.scalars(
            select(Order.deposit_address).where(
                Order.status == OrderStatus.AWAITING_DEPOSIT.value))).all())
    for address, watermark in plan.items():
        # each address in its own try/except: a throttle on one (or a TronGrid
        # hiccup) must not skip the remaining addresses, the BEP20 sweep,
        # reminders, or order expiry below
        try:
            transfers = await fetch_transfers(http, address, watermark)
        except Exception:
            log.warning("confirmed fetch failed for one address; other work "
                        "continues", exc_info=True)
            continue
        for tx in transfers:
            if int(tx.get("block_timestamp", 0) or 0) <= watermark:
                continue
            await _credit_or_hold(bot, tx, address)
        # instant feedback: sight the not-yet-confirmed transfers too, but only
        # where an order is actually waiting on this address
        if address in awaiting:
            try:
                for tx in await fetch_transfers(http, address, watermark,
                                                confirmed=False):
                    await _note_unconfirmed(bot, tx, address)
            except Exception:
                log.warning("unconfirmed sweep failed; confirmed path "
                            "unaffected", exc_info=True)
    _prune_seen()
    await scan_bsc_once(bot, http)          # BEP20, if configured
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
            # credit the CONFIRMED transfers first — this is what the tap is
            # really for. The unconfirmed "we see it" sweep is best-effort and
            # isolated so its failure (e.g. a 429 on the doubled call) can
            # never discard an already-fetched confirmed deposit.
            for tx in transfers:
                if int(tx.get("block_timestamp", 0) or 0) <= watermark:
                    continue
                await _credit_or_hold(bot, tx, address)
            try:
                for tx in await fetch_transfers(http, address, watermark,
                                                confirmed=False):
                    await _note_unconfirmed(bot, tx, address)
            except Exception:
                log.warning("on-demand unconfirmed sweep failed; confirmed "
                            "path unaffected", exc_info=True)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as http:
            await scan_bsc_once(bot, http)   # also sweep BEP20 on demand (once)
    except Exception:
        log.exception("on-demand check failed for order %s", order_id)
    async with Session() as session:
        o = await session.get(Order, order_id)
        return o.status if o else None


async def lookup_claim_tx(txid: str, address: str, since_ms: int) -> dict:
    """Look up a user-submitted TXID on-chain to help the admin verify a
    late/missed payment: is it a confirmed USDT transfer TO our address, for how
    much, and when? Routes by hash shape (0x… → BEP20/BscScan, else TRC20).
    Returns {found, error, amount, to, to_ok, timestamp}."""
    from .helpers import is_bsc_txid
    if is_bsc_txid(txid):
        return await lookup_bsc_tx(txid, since_ms)
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


async def lookup_tx_global(txid: str, our_address: str) -> dict:
    """Global per-tx lookup BY HASH (any destination), so a claim/refund can be
    declined on the user's face when the tx wasn't actually sent to us. Unlike
    lookup_claim_tx (which only searches OUR address's transfers), this reports
    the tx's REAL destination. Returns {found, error, to, to_ok, amount, timestamp(ms)}."""
    from .helpers import is_bsc_txid
    blank = {"found": False, "error": False, "to": "", "to_ok": False,
             "amount": 0.0, "timestamp": 0}
    if is_bsc_txid(txid):
        # BEP20: address-scoped lookup (a BscScan global receipt parse is heavier);
        # a BEP20 hash sent elsewhere simply reads as not-found here.
        r = await lookup_bsc_tx(txid, 0)
        return {**blank, **(r or {})}
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as http:
            async with http.get(f"{settings.tronscan_api}/transaction-info",
                                params={"hash": txid}) as resp:
                if resp.status != 200:
                    return {**blank, "error": True}
                data = await resp.json()
    except Exception as e:
        log.warning("tron global lookup failed: %s", type(e).__name__)
        return {**blank, "error": True}
    if not data or not data.get("hash"):
        return blank                       # genuinely not on-chain
    ts = int(data.get("timestamp", 0) or 0)     # ms
    best = None
    for tr in (data.get("trc20TransferInfo") or []):
        if (tr.get("contract_address") or "") != settings.usdt_contract:
            continue
        to = tr.get("to_address") or ""
        try:
            amt = int(tr.get("amount_str", "0") or 0) / (10 ** int(tr.get("decimals", 6) or 6))
        except (TypeError, ValueError):
            amt = 0.0
        if to == our_address:
            best = {"to": to, "amount": amt}
            break
        best = best or {"to": to, "amount": amt}
    if best is None:                       # tx exists but no USDT transfer in it
        return {**blank, "found": True, "timestamp": ts}
    return {"found": True, "error": False, "to": best["to"],
            "to_ok": best["to"] == our_address, "amount": best["amount"], "timestamp": ts}


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
