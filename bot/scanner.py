"""TRON auto-scan: polls TronGrid for USDT (TRC20) transfers into the desk's
deposit address, matches each new transfer to the oldest awaiting order with
the same amount, and drives the deposit-confirmed step. Also expires orders
whose deposit never arrived."""

import asyncio
import logging
from datetime import timedelta

import aiohttp
from aiogram import Bot
from sqlalchemy import func, select

from . import texts
from .config import settings
from .db import Session, get_deposit_address
from .flow import notify_deposit_received
from .helpers import notify_admins, notify_user, try_transition
from .models import Order, OrderStatus, SeenTx, User, utcnow

log = logging.getLogger(__name__)

AMOUNT_TOLERANCE = 0.005  # USDT — exact match with float slack


async def fetch_transfers(http: aiohttp.ClientSession, address: str) -> list[dict]:
    url = f"{settings.trongrid_url}/v1/accounts/{address}/transactions/trc20"
    params = {
        "only_to": "true",
        "only_confirmed": "true",
        "limit": "50",
        "contract_address": settings.usdt_contract,
    }
    headers = {"TRON-PRO-API-KEY": settings.trongrid_key} if settings.trongrid_key else {}
    async with http.get(url, params=params, headers=headers) as resp:
        resp.raise_for_status()
        payload = await resp.json()
    return payload.get("data") or []


def transfer_amount(tx: dict) -> float | None:
    token = tx.get("token_info") or {}
    if token.get("address") and token["address"] != settings.usdt_contract:
        return None
    try:
        decimals = int(token.get("decimals", 6))
        return int(tx.get("value", "0")) / (10 ** decimals)
    except (TypeError, ValueError):
        return None


async def process_transfer(bot: Bot, tx: dict, address: str, bootstrap: bool) -> None:
    txid = tx.get("transaction_id")
    amount = transfer_amount(tx)
    if not txid or amount is None or amount <= 0:
        return
    if (tx.get("to") or "") != address:
        return
    matched_id: int | None = None
    async with Session() as session:
        if await session.get(SeenTx, txid) is not None:
            return
        if not bootstrap:
            candidate = await session.scalar(
                select(Order).where(
                    Order.status == OrderStatus.AWAITING_DEPOSIT.value,
                    Order.usd_amount >= amount - AMOUNT_TOLERANCE,
                    Order.usd_amount <= amount + AMOUNT_TOLERANCE,
                ).order_by(Order.id).limit(1))
            if candidate is not None:
                updated = await try_transition(
                    session, candidate.id,
                    (OrderStatus.AWAITING_DEPOSIT,), OrderStatus.DEPOSIT_RECEIVED,
                    txid=txid, deposit_detected_at=utcnow())
                if updated is not None:
                    matched_id = updated.id
        session.add(SeenTx(txid=txid, amount=amount, order_id=matched_id))
        await session.commit()
    if bootstrap:
        return
    if matched_id is not None:
        await notify_deposit_received(bot, matched_id)
    else:
        await notify_admins(bot,
                            f"⚠️ Unmatched deposit: <b>{amount:g} USDT</b> "
                            f"(tx <code>{txid}</code>) — no awaiting order for this "
                            f"amount. Handle manually.")


async def expire_stale_orders(bot: Bot) -> None:
    cutoff = utcnow() - timedelta(minutes=settings.deposit_ttl_min)
    expired: list[tuple[Order, str]] = []
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
                expired.append((updated, user.lang if user and user.lang else "en"))
    for order, lang in expired:
        await notify_user(bot, order.user_id, texts.order_expired(order.id, lang))


async def scan_once(bot: Bot, http: aiohttp.ClientSession, bootstrap: bool) -> None:
    async with Session() as session:
        address = await get_deposit_address(session)
    if not address:
        return
    transfers = await fetch_transfers(http, address)
    for tx in reversed(transfers):  # oldest first, so FIFO matching holds
        await process_transfer(bot, tx, address, bootstrap)
    await expire_stale_orders(bot)


async def scan_loop(bot: Bot) -> None:
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as http:
        async with Session() as session:
            seen = await session.scalar(select(func.count()).select_from(SeenTx))
        # first ever run: ingest the address's existing history silently so
        # old transfers are never credited or alerted on
        bootstrap = not seen
        while True:
            try:
                await scan_once(bot, http, bootstrap)
                bootstrap = False
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tron scan failed; retrying next tick")
            await asyncio.sleep(settings.scan_interval_sec)
