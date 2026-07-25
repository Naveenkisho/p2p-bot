"""Optional QR-code generation for the deposit address.

Kept dependency-optional on purpose: if `qrcode`/Pillow aren't installed the bot
still runs and simply skips the QR image (so a plain `git pull && restart` never
breaks). Install with `pip install "qrcode[pil]"` to turn the QR on.
"""
import io
import logging

log = logging.getLogger(__name__)


def qr_png(data: str) -> bytes | None:
    """A PNG QR encoding `data` (a deposit address), or None if unavailable."""
    if not data:
        return None
    try:
        import qrcode
    except Exception:
        return None
    try:
        img = qrcode.make(data)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        log.warning("QR generation failed")
        return None
