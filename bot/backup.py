"""Automatic rotating SQLite backups.

The whole desk lives in ONE file (accounts, orders, bank cards, tickets, and
every panel setting). These snapshots are the safety net against an accidental
delete, disk failure, or a bad deploy — taken with SQLite's online-backup API
so each copy is a consistent point-in-time snapshot even while the bot writes.
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

BACKUP_KEEP = 14            # rotating copies to retain
BACKUP_INTERVAL = 24 * 3600
_NAME_RE = None


def backups_dir() -> Path:
    d = Path(settings.db_path).resolve().parent / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_sync(stamp: str) -> str:
    src = str(Path(settings.db_path).resolve())
    if not os.path.exists(src):
        return ""
    dst = backups_dir() / f"p2p-{stamp}.sqlite3"
    con = sqlite3.connect(src)
    try:
        bck = sqlite3.connect(str(dst))
        try:
            with bck:
                con.backup(bck)      # consistent online snapshot
        finally:
            bck.close()
    finally:
        con.close()
    # prune oldest beyond the retention window
    files = sorted(backups_dir().glob("p2p-*.sqlite3"))
    for old in files[:-BACKUP_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return dst.name


async def make_backup(stamp: str) -> str:
    """Write one snapshot (off the event loop); returns its filename or ''."""
    return await asyncio.to_thread(_make_sync, stamp)


def list_backups() -> list[dict]:
    out = []
    for p in sorted(backups_dir().glob("p2p-*.sqlite3"), reverse=True):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    return out


def backup_path(name: str) -> Path | None:
    """Resolve a backup filename to a real path, refusing anything that isn't a
    plain p2p-*.sqlite3 file inside the backups dir (no traversal)."""
    if "/" in name or "\\" in name or ".." in name:
        return None
    if not (name.startswith("p2p-") and name.endswith(".sqlite3")):
        return None
    p = backups_dir() / name
    return p if p.is_file() else None


async def send_db_backup(force: bool = False) -> bool:
    """Off-site copy: gzip today's snapshot and email it to the operator (same
    recipient as the daily SEO report). Local rotation above survives a bad
    deploy or an accidental delete — this survives the SERVER dying, because
    the copy lives in the operator's mailbox. Once per day unless forced."""
    import gzip

    from .db import Session, get_setting, set_setting
    from . import sender
    from .seo_report import IST

    async with Session() as s:
        to_addr = (await get_setting(s, "seo_report_email") or "").strip()
        last = await get_setting(s, "db_backup_mailed") or ""
    today_key = datetime.now(IST).strftime("%Y-%m-%d")
    if not to_addr or (last == today_key and not force):
        return False

    def _snap_gz() -> bytes:
        src = sqlite3.connect(str(Path(settings.db_path).resolve()))
        try:
            mem = sqlite3.connect(":memory:")
            try:
                with mem:
                    src.backup(mem)
                raw = b"".join(
                    (line + "\n").encode() for line in mem.iterdump())
            finally:
                mem.close()
        finally:
            src.close()
        return gzip.compress(raw, 6)

    # SQL-text dump (iterdump) restores with plain `sqlite3 db < file` and
    # compresses far better than the binary file
    gz = await asyncio.to_thread(_snap_gz)
    size_mb = len(gz) / 1e6
    inner = (f"<p>Daily database backup attached ({size_mb:.1f} MB "
             "compressed). This one file is the whole desk — every user, "
             "order, bank, email and setting. Keep a few recent copies.</p>"
             "<p><b>Restore on a fresh server:</b> install the bot, then<br>"
             "<code>gunzip backup.sql.gz &amp;&amp; sqlite3 "
             f"{settings.db_path} &lt; backup.sql</code><br>"
             "and start the bot — everything comes back exactly as it was.</p>")
    attachments = [(f"backup-{today_key}.sql.gz", gz, "application/gzip")]
    if len(gz) > 20 * 1024 * 1024:
        inner = (f"<p>Today's backup is too large to email ({size_mb:.1f} MB "
                 "compressed). Download it from the panel's Backups tab and "
                 "store it off the server.</p>")
        attachments = None
    ok = await sender.send_transactional(
        to_addr, "", f"Desk backup — {today_key}", inner,
        stream="mkt", attachments=attachments)
    if ok:
        async with Session() as s:
            await set_setting(s, "db_backup_mailed", today_key)
            await s.commit()
        log.info("db backup emailed to %s (%.1f MB)", to_addr, size_mb)
    return ok


async def backup_loop() -> None:
    """Background loop: a snapshot on boot, then daily. Never crashes the app."""
    while True:
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
            name = await make_backup(stamp)
            if name:
                log.info("db backup written: %s", name)
        except Exception:
            log.exception("db backup failed")
        try:
            await send_db_backup()
        except Exception:
            log.exception("db backup email failed")
        await asyncio.sleep(BACKUP_INTERVAL)
