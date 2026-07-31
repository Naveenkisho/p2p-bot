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
        await asyncio.sleep(BACKUP_INTERVAL)
