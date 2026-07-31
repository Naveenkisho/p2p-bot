"""Bulk outbound messaging — email via SMTP (Brevo) and SMS via MSG91.

Both channels stay completely OFF until credentials are entered in the panel's
Messaging tab, so the desk runs exactly as before until a provider is wired in
deliberately. Sends run as a single background task with throttling and a hard
per-run cap, honour email unsubscribes, and never block the bot / scanner /
website event loop (SMTP is blocking, so it runs in a worker thread; MSG91 is
called over the shared aiohttp stack).

Design notes
------------
* Credentials live in the same Setting table as the bot token and BscScan key,
  so there's nothing new to host or back up.
* Only one email job and one SMS job can run at a time — a second click while a
  run is in flight is refused, never doubled.
* Every email carries a one-click List-Unsubscribe header (RFC 8058) plus a
  visible footer link; the public /unsubscribe route records the opt-out and
  the recipient list skips it forever after.
"""

from __future__ import annotations

import asyncio
import hmac
import html as _html
import json
import logging
import re
import secrets
import smtplib
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from urllib.parse import quote

import aiohttp
from sqlalchemy import select

from .config import settings
from .db import Session, get_setting, set_setting, site_secret
from .helpers import unsub_token
from .models import Account, Unsubscribe, User

log = logging.getLogger(__name__)

# Runaway guards — a single mistaken click can't blast an unbounded list. These
# are far above any realistic signup count; a truncated run is logged loudly.
EMAIL_MAX_PER_RUN = 5000
SMS_MAX_PER_RUN = 5000
EMAIL_DELAY = 0.2       # seconds between messages — gentle on the SMTP relay
SMS_BATCH = 100         # MSG91 accepts many recipients per call
SMTP_TIMEOUT = 30

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ── credentials ──────────────────────────────────────────────────────────────

async def email_config() -> dict:
    async with Session() as s:
        cfg = {
            "host": (await get_setting(s, "email_smtp_host") or "").strip(),
            "port": (await get_setting(s, "email_smtp_port") or "587").strip(),
            "user": (await get_setting(s, "email_smtp_user") or "").strip(),
            "pw": (await get_setting(s, "email_smtp_pass") or ""),
            "from_addr": (await get_setting(s, "email_from") or "").strip(),
            "from_name": (await get_setting(s, "email_from_name") or "").strip(),
            # optional per-stream senders — blank falls back to from_addr. Any
            # @<authenticated-domain> address works once the domain is verified
            # at the SMTP provider; splitting keeps marketing complaints from
            # dragging down OTP/receipt deliverability.
            "from_tx": (await get_setting(s, "email_from_tx") or "").strip(),
            "from_otp": (await get_setting(s, "email_from_otp") or "").strip(),
            "from_mkt": (await get_setting(s, "email_from_mkt") or "").strip(),
        }
    return cfg


def _stream_cfg(cfg: dict, stream: str) -> dict:
    """cfg with From switched to the stream's sender ('tx' | 'otp' | 'mkt').
    When the stream address differs from the main one, Reply-To points back at
    the main (support) address so customer replies always reach a human."""
    addr = (cfg.get(f"from_{stream}") or "").strip()
    if not addr or not _EMAIL_RE.match(addr) or addr == cfg["from_addr"]:
        return cfg
    out = dict(cfg)
    out["from_addr"] = addr
    out["reply_to"] = cfg["from_addr"]
    return out


def email_ready(cfg: dict) -> bool:
    return bool(cfg["host"] and cfg["user"] and cfg["pw"] and cfg["from_addr"]
                and _EMAIL_RE.match(cfg["from_addr"]))


async def sms_config() -> dict:
    async with Session() as s:
        cfg = {
            "authkey": (await get_setting(s, "sms_msg91_authkey") or "").strip(),
            "sender": (await get_setting(s, "sms_msg91_sender") or "").strip(),
            "template": (await get_setting(s, "sms_msg91_template") or "").strip(),
            "var": (await get_setting(s, "sms_msg91_var") or "var1").strip() or "var1",
        }
    return cfg


def sms_ready(cfg: dict) -> bool:
    return bool(cfg["authkey"] and cfg["sender"] and cfg["template"])


# ── recipients ───────────────────────────────────────────────────────────────

async def email_recipients() -> list[tuple[str, str]]:
    """(email, name) for every website account with a VERIFIED email, minus any
    that unsubscribed. De-duplicated case-insensitively. Unverified addresses
    (no OTP entered) never receive a single message — fake signups are inert."""
    async with Session() as s:
        accounts = (await s.scalars(select(Account))).all()
        unsub = {e.lower() for e in
                 (await s.scalars(select(Unsubscribe.email))).all()}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in accounts:
        raw = (a.email or "").strip()
        low = raw.lower()
        if (raw and _EMAIL_RE.match(raw) and getattr(a, "email_verified", False)
                and low not in seen and low not in unsub):
            seen.add(low)
            out.append((raw, a.name or ""))
    return out


def norm_msisdn(phone: str) -> str:
    """India-normalise a stored phone into MSG91's country-code+number form
    (digits only, e.g. 919812345678). Returns '' for anything that isn't a
    plausible 10-digit Indian mobile."""
    # ASCII 0-9 only — str.isdigit()/\d also match Arabic-Indic, Devanagari etc.,
    # which would produce a non-ASCII 'mobiles' value MSG91 rejects.
    digits = "".join(ch for ch in (phone or "") if "0" <= ch <= "9")
    if len(digits) == 10 and digits[0] in "6789":
        return "91" + digits
    if len(digits) == 11 and digits.startswith("0") and digits[1] in "6789":
        return "91" + digits[1:]
    if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
        return digits
    return ""


async def sms_recipients() -> list[str]:
    async with Session() as s:
        accounts = (await s.scalars(select(Account))).all()
    out: list[str] = []
    seen: set[str] = set()
    for a in accounts:
        m = norm_msisdn(a.phone)
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


# ── email send (blocking; runs in a worker thread) ───────────────────────────

def _unsub_url(email: str, secret: bytes) -> str:
    base = (settings.site_url or "").rstrip("/")
    if not base:
        return ""
    return (f"{base}/unsubscribe?e={quote(email)}"
            f"&t={unsub_token(secret, email)}")


def _build_message(cfg: dict, to_addr: str, to_name: str, subject: str,
                   body: str, is_html: bool, unsub_url: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((cfg["from_name"] or None, cfg["from_addr"]))
    msg["To"] = formataddr((to_name or None, to_addr))
    # header-injection safety: no CR/LF ever reaches a header
    msg["Subject"] = subject.replace("\r", " ").replace("\n", " ").strip()
    # RFC 5322 Date is mandatory and Message-ID is expected — set them ourselves
    # so mail isn't spam-scored/rejected on relays that don't backfill them.
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(cfg["from_addr"].split("@")[-1] or None))
    if cfg.get("reply_to"):
        msg["Reply-To"] = cfg["reply_to"]
    if unsub_url:
        msg["List-Unsubscribe"] = f"<{unsub_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    foot_txt = f"\n\n----\nUnsubscribe: {unsub_url}" if unsub_url else ""
    foot_html = (
        f'<hr style="margin-top:28px;border:none;border-top:1px solid #ddd">'
        f'<p style="font-size:12px;color:#888">You are receiving this because '
        f'you signed up on our site. '
        f'<a href="{unsub_url}">Unsubscribe</a>.</p>' if unsub_url else "")
    if is_html:
        # Text fallback for no-HTML clients / spam filters: drop <style>/<script>
        # blocks WITH their contents first (else raw CSS/JS leaks into the text
        # part), then strip remaining tags, unescape entities, tidy whitespace.
        no_blocks = re.sub(r"(?is)<(style|script)\b.*?</\1>", " ", body)
        text_alt = _html.unescape(re.sub(r"<[^>]+>", " ", no_blocks))
        text_alt = re.sub(r"[ \t ]+", " ", text_alt)
        text_alt = re.sub(r"\n\s*\n\s*\n+", "\n\n", text_alt).strip()
        msg.set_content(text_alt + foot_txt)
        msg.add_alternative(body + foot_html, subtype="html")
    else:
        html_body = "<p>" + _html.escape(body).replace("\n", "<br>") + "</p>"
        msg.set_content(body + foot_txt)
        msg.add_alternative(html_body + foot_html, subtype="html")
    return msg


def _smtp_connect(cfg: dict) -> smtplib.SMTP:
    port = int(cfg["port"] or "587")
    if port == 465:
        srv = smtplib.SMTP_SSL(cfg["host"], port, timeout=SMTP_TIMEOUT,
                               context=ssl.create_default_context())
    else:
        srv = smtplib.SMTP(cfg["host"], port, timeout=SMTP_TIMEOUT)
        srv.ehlo()
        srv.starttls(context=ssl.create_default_context())
        srv.ehlo()
    srv.login(cfg["user"], cfg["pw"])
    return srv


def _send_batch_blocking(cfg: dict, recipients: list[tuple[str, str]],
                         subject: str, body: str, is_html: bool,
                         secret: bytes, progress) -> tuple[int, int, list[str]]:
    """Open one SMTP connection and send to every recipient. `progress(sent,
    failed)` is called after each message so the panel can show live counts.
    An empty `secret` means transactional mail: no unsubscribe link or header.
    Returns (sent, failed, first_errors)."""
    sent = failed = 0
    errors: list[str] = []
    srv = _smtp_connect(cfg)
    try:
        for to_addr, to_name in recipients:
            try:
                url = _unsub_url(to_addr, secret) if secret else ""
                msg = _build_message(cfg, to_addr, to_name, subject, body,
                                     is_html, url)
                srv.send_message(msg)
                sent += 1
            except Exception as e:   # one bad address never stops the run
                failed += 1
                if len(errors) < 5:
                    errors.append(f"{to_addr}: {e}")
                log.warning("bulk email to %s failed: %s", to_addr, e)
            progress(sent, failed)
            time.sleep(EMAIL_DELAY)
    finally:
        try:
            srv.quit()
        except Exception:
            pass
    return sent, failed, errors


# ── SMS send (async; MSG91 flow API) ─────────────────────────────────────────

async def _send_sms_blocking(cfg: dict, recipients: list[str], message: str,
                             progress) -> tuple[int, int, list[str]]:
    url = "https://control.msg91.com/api/v5/flow/"
    headers = {"authkey": cfg["authkey"], "Content-Type": "application/json",
               "accept": "application/json"}
    sent = failed = 0
    errors: list[str] = []
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for i in range(0, len(recipients), SMS_BATCH):
            chunk = recipients[i:i + SMS_BATCH]
            payload = {
                "template_id": cfg["template"],
                "sender": cfg["sender"],
                "short_url": "0",
                "recipients": [{"mobiles": m, cfg["var"]: message} for m in chunk],
            }
            try:
                async with session.post(url, json=payload, headers=headers) as r:
                    text = await r.text()
                    try:
                        data = json.loads(text)
                    except ValueError:
                        data = None
                    ok = (r.status == 200 and isinstance(data, dict)
                          and str(data.get("type", "")).lower() == "success")
                    if ok:
                        sent += len(chunk)
                    else:
                        failed += len(chunk)
                        if len(errors) < 5:
                            errors.append(f"HTTP {r.status}: {text[:180]}")
                        log.warning("MSG91 batch failed: HTTP %s %s", r.status, text[:200])
            except Exception as e:
                failed += len(chunk)
                if len(errors) < 5:
                    errors.append(str(e))
                log.warning("MSG91 batch error: %s", e)
            progress(sent, failed)
    return sent, failed, errors


# ── job state + background runners ───────────────────────────────────────────

def _blank_job() -> dict:
    return {"running": False, "channel": "", "sent": 0, "failed": 0,
            "total": 0, "started": 0.0, "finished": 0.0, "errors": []}


_email_job = _blank_job()
_sms_job = _blank_job()
# Keep strong refs to in-flight broadcast tasks — the loop only holds a weak one,
# so an unreferenced task could be GC'd mid-run, leaving a channel locked.
_bg_tasks: set = set()


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


def email_status() -> dict:
    return dict(_email_job)


def sms_status() -> dict:
    return dict(_sms_job)


async def send_test_email(to_addr: str, subject: str, body: str,
                          is_html: bool) -> tuple[bool, str]:
    """Synchronous single send so the panel can confirm the creds work before a
    full blast. Returns (ok, detail)."""
    cfg = await email_config()
    if not email_ready(cfg):
        return False, "Email isn't configured yet — save your SMTP details first."
    if not _EMAIL_RE.match((to_addr or "").strip()):
        return False, "Enter a valid test address."
    cfg = _stream_cfg(cfg, "mkt")   # campaigns preview from the marketing sender
    secret = await site_secret()

    def _noop(_s, _f):
        pass

    try:
        sent, failed, errors = await asyncio.to_thread(
            _send_batch_blocking, cfg, [((to_addr or "").strip(), "")],
            subject, body, is_html, secret, _noop)
    except Exception as e:
        return False, f"SMTP error: {e}"
    if sent:
        return True, f"Test email sent to {to_addr}."
    return False, (errors[0] if errors else "Send failed — check the SMTP details.")


async def send_test_sms(to_number: str, message: str) -> tuple[bool, str]:
    cfg = await sms_config()
    if not sms_ready(cfg):
        return False, "SMS isn't configured yet — save your MSG91 details first."
    m = norm_msisdn(to_number)
    if not m:
        return False, "Enter a valid 10-digit Indian mobile number."

    def _noop(_s, _f):
        pass

    sent, failed, errors = await _send_sms_blocking(cfg, [m], message, _noop)
    if sent:
        return True, f"Test SMS sent to {to_number}."
    return False, (errors[0] if errors else "Send failed — check the MSG91 details.")


async def start_email_broadcast(subject: str, body: str, is_html: bool,
                                bot=None) -> tuple[bool, str]:
    """Kick off a background send to every account email. Returns (started,
    message). Refuses if a run is already in flight or nothing's configured."""
    if _email_job["running"]:
        return False, "An email send is already running — wait for it to finish."
    cfg = _stream_cfg(await email_config(), "mkt")
    if not email_ready(cfg):
        return False, "Email isn't configured yet — save your SMTP details first."
    if not (settings.site_url or "").strip():
        return False, ("Set the site URL (P2P_SITE_URL) first — every bulk email "
                       "must carry a working one-click unsubscribe link, and Gmail "
                       "and Yahoo reject bulk mail without it.")
    recipients = await email_recipients()
    if not recipients:
        return False, "No subscribed email addresses to send to yet."
    truncated = len(recipients) > EMAIL_MAX_PER_RUN
    if truncated:
        log.warning("email broadcast truncated: %s recipients capped at %s",
                    len(recipients), EMAIL_MAX_PER_RUN)
        recipients = recipients[:EMAIL_MAX_PER_RUN]
    secret = await site_secret()

    # Atomic claim — NO await between this re-check and create_task, so two
    # clicks that both passed the awaits above can never both spawn a run.
    if _email_job["running"]:
        return False, "An email send is already running — wait for it to finish."
    _email_job.update(_blank_job())
    _email_job.update({"running": True, "channel": "email",
                       "total": len(recipients), "started": time.time()})

    def _progress(s, f):
        _email_job["sent"] = s
        _email_job["failed"] = f

    async def _run():
        try:
            sent, failed, errors = await asyncio.to_thread(
                _send_batch_blocking, cfg, recipients, subject, body,
                is_html, secret, _progress)
            _email_job["sent"] = sent
            _email_job["failed"] = failed
            _email_job["errors"] = errors
        except Exception as e:
            log.exception("email broadcast crashed")
            _email_job["errors"] = [str(e)]
        finally:
            _email_job["running"] = False
            _email_job["finished"] = time.time()
            await _notify(bot, "Email", _email_job)

    _spawn(_run())
    extra = f" (capped at {EMAIL_MAX_PER_RUN})" if truncated else ""
    return True, f"Sending to {len(recipients)} subscribers{extra}…"


async def start_sms_broadcast(message: str, bot=None) -> tuple[bool, str]:
    if _sms_job["running"]:
        return False, "An SMS send is already running — wait for it to finish."
    cfg = await sms_config()
    if not sms_ready(cfg):
        return False, "SMS isn't configured yet — save your MSG91 details first."
    recipients = await sms_recipients()
    if not recipients:
        return False, "No valid phone numbers to send to yet."
    truncated = len(recipients) > SMS_MAX_PER_RUN
    if truncated:
        log.warning("SMS broadcast truncated: %s recipients capped at %s",
                    len(recipients), SMS_MAX_PER_RUN)
        recipients = recipients[:SMS_MAX_PER_RUN]

    # Atomic claim — see start_email_broadcast: no await until create_task.
    if _sms_job["running"]:
        return False, "An SMS send is already running — wait for it to finish."
    _sms_job.update(_blank_job())
    _sms_job.update({"running": True, "channel": "sms",
                     "total": len(recipients), "started": time.time()})

    def _progress(s, f):
        _sms_job["sent"] = s
        _sms_job["failed"] = f

    async def _run():
        try:
            sent, failed, errors = await _send_sms_blocking(
                cfg, recipients, message, _progress)
            _sms_job["sent"] = sent
            _sms_job["failed"] = failed
            _sms_job["errors"] = errors
        except Exception as e:
            log.exception("SMS broadcast crashed")
            _sms_job["errors"] = [str(e)]
        finally:
            _sms_job["running"] = False
            _sms_job["finished"] = time.time()
            await _notify(bot, "SMS", _sms_job)

    _spawn(_run())
    extra = f" (capped at {SMS_MAX_PER_RUN})" if truncated else ""
    return True, f"Sending to {len(recipients)} numbers{extra}…"


async def _notify(bot, label: str, job: dict) -> None:
    if bot is None:
        return
    try:
        from .helpers import notify_admins
        line = (f"📣 {label} broadcast finished — {job['sent']} sent, "
                f"{job['failed']} failed of {job['total']}.")
        if job["errors"]:
            line += f"\nFirst error: {job['errors'][0]}"
        await notify_admins(bot, line)
    except Exception:
        log.exception("broadcast completion notify failed")


# ── transactional emails (order lifecycle) + auto rate updates ───────────────
# Transactional mail (order confirmations/receipts) goes to the ONE customer an
# order belongs to, regardless of the marketing unsubscribe list — exactly like
# an exchange still emails you your receipts after you opt out of promos. It
# carries no marketing footer. The rate-update blast, by contrast, IS marketing:
# it reuses start_email_broadcast, so it honours unsubscribes and the one-job
# lock, and it dedupes + rate-limits itself so an admin fiddling with rates
# can't accidentally spam the list.

_ACCT_BASE = 1 << 48
RATE_BLAST_GAP = 30 * 60      # min seconds between auto rate emails
_rate_retry = {"pending": False}

_BRAND_NAME = "IndiaXchange"
_C_NAVY = "#0e1330"
_C_GREEN = "#00c26f"
_C_GREEN_DARK = "#00a85f"
_C_INK = "#3c4761"
_C_SOFT = "#e1f9ee"
_C_OK = "#0c8f56"


def _inr(v: float) -> str:
    return f"₹{v:,.2f}"


def _usd(v: float) -> str:
    # no thousands separator: this string is what the customer must SEND
    # exactly — "1,000.07" invites a mistyped amount that never matches
    return f"{v:.2f}"


def brand_wrap(inner: str, contact: str = "", legal: bool = False,
               tg_handle: str = "") -> str:
    """Shared email chrome: navy header band with the ₹ mark + wordmark, white
    card, muted footer. Inline-styled tables so Gmail/Outlook render it.
    legal=True appends the professional compliance footer (links row, security
    notice, copyright) — used on the order lifecycle emails only."""
    site = (settings.site_url or "").rstrip("/")
    contact_line = (f"<a href='mailto:{_html.escape(contact)}' "
                    f"style='color:{_C_GREEN_DARK};text-decoration:none'>"
                    f"{_html.escape(contact)}</a>" if contact else "the support desk")
    extra = ""
    if legal:
        links = []
        if site:
            links.append(f"<a href='{_html.escape(site)}' style='color:{_C_GREEN_DARK};"
                         f"text-decoration:none;font-weight:bold'>"
                         f"{_html.escape(site.split('://')[-1])}</a>")
        if tg_handle:
            links.append(f"<a href='https://t.me/{_html.escape(tg_handle)}' "
                         f"style='color:{_C_GREEN_DARK};text-decoration:none;"
                         f"font-weight:bold'>Telegram support</a>")
        if contact:
            links.append(f"<a href='mailto:{_html.escape(contact)}' "
                         f"style='color:{_C_GREEN_DARK};text-decoration:none;"
                         f"font-weight:bold'>{_html.escape(contact)}</a>")
        links_row = " &nbsp;&middot;&nbsp; ".join(links)
        year = datetime.now(timezone.utc).year
        extra = f"""<p style="margin:10px 0 0;color:#8b95a8;font-size:12px;line-height:1.7">{links_row}</p>
<p style="margin:12px 0 0;color:#9aa3b8;font-size:11px;line-height:1.7">
<strong>Security notice:</strong> {_BRAND_NAME} will never ask for your password
or verification codes, and will never ask you to send USDT to any address other
than the one shown on your own order page. Genuine mail from us always comes
from an address ending in <strong>@{_html.escape(site.split('://')[-1]) if site else 'our domain'}</strong>.
If anything looks off, forward the email to {contact_line} before acting.</p>
<p style="margin:12px 0 0;color:#9aa3b8;font-size:11px;line-height:1.6">
&copy; {year} {_BRAND_NAME}. All rights reserved. This message was sent to you
about an order or account activity you initiated.</p>"""
    return f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f7fa;margin:0;padding:24px 0;font-family:Arial,Helvetica,sans-serif">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px;max-width:600px;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid #e6eaf1">
<tr><td style="background:{_C_NAVY};padding:20px 28px">
<table role="presentation" cellpadding="0" cellspacing="0"><tr>
<td style="width:38px;height:38px;border:2px solid {_C_GREEN};border-radius:9px;text-align:center;color:{_C_GREEN};font-size:19px;font-weight:bold;line-height:38px">&#8377;</td>
<td style="padding-left:12px;color:#ffffff;font-size:19px;font-weight:bold">India<span style="color:{_C_GREEN}">Xchange</span></td>
</tr></table></td></tr>
<tr><td style="padding:30px 28px 26px">{inner}</td></tr>
<tr><td style="padding:18px 28px 22px;border-top:1px solid #e6eaf1">
<p style="margin:0;color:#8b95a8;font-size:12px;line-height:1.6">
<strong style="color:#5a657d">{_BRAND_NAME}</strong> &mdash; USDT&nbsp;&rarr;&nbsp;INR trading desk<br>
Questions? Just reply to this email or write to {contact_line}.{f"<br>{_html.escape(site)}" if site and not legal else ""}</p>
{extra}</td></tr></table></td></tr></table>"""


def _btn(href: str, label: str) -> str:
    return (f"<table role='presentation' cellpadding='0' cellspacing='0'><tr>"
            f"<td align='center' style='background:{_C_GREEN};border-radius:10px'>"
            f"<a href='{_html.escape(href)}' style='display:inline-block;padding:13px 28px;"
            f"color:#062b1a;font-size:15px;font-weight:bold;text-decoration:none'>"
            f"{label}</a></td></tr></table>")


def _kv_rows(rows: list[tuple[str, str]]) -> str:
    """Label/value detail table (values pre-escaped by callers where dynamic)."""
    tr = "".join(
        f"<tr><td style='padding:7px 14px;color:#5a657d;font-size:14px;white-space:nowrap'>{k}</td>"
        f"<td style='padding:7px 14px;color:{_C_NAVY};font-size:14px;font-weight:bold' align='right'>{v}</td></tr>"
        for k, v in rows)
    return (f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
            f"style='background:#f4f7fa;border-radius:12px;margin:14px 0 18px'>{tr}</table>")


def _badge(glyph: str, bg: str = _C_GREEN, fg: str = "#ffffff") -> str:
    """Round status mark built from a text glyph (✓ ↓ ₹) — NOT an emoji, so it
    renders identically in every mail client and prints cleanly."""
    return (f"<table role='presentation' cellpadding='0' cellspacing='0' "
            f"style='margin:0 0 14px'><tr><td style='width:46px;height:46px;"
            f"background:{bg};border-radius:23px;text-align:center;vertical-align:middle;"
            f"color:{fg};font-size:22px;font-weight:bold;line-height:46px'>{glyph}</td>"
            f"</tr></table>")


def _ist_now_str() -> str:
    ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    return ist.strftime("%d %b %Y, %I:%M %p") + " IST"


def _ld_order(tag: str, service_label: str, inr: float, status: str) -> str:
    """schema.org Order markup — lets Gmail render its native 'Ordered from /
    Items' summary card above the email (the professional look big senders
    have). Harmless if a client ignores it. Only code-built values go in —
    never user-typed text — so no escaping/injection surface."""
    data = {
        "@context": "http://schema.org",
        "@type": "Order",
        "merchant": {"@type": "Organization", "name": _BRAND_NAME},
        "orderNumber": tag.lstrip("#"),
        "orderStatus": f"http://schema.org/{status}",
        "priceCurrency": "INR",
        "price": f"{inr:.2f}",
        "acceptedOffer": {
            "@type": "Offer",
            "itemOffered": {"@type": "Product",
                            "name": f"USDT → INR bank payout via {service_label}"},
            "price": f"{inr:.2f}",
            "priceCurrency": "INR",
            "eligibleQuantity": {"@type": "QuantitativeValue", "value": 1},
        },
    }
    if settings.site_url:
        data["url"] = settings.site_url
    return ("<script type='application/ld+json'>"
            + json.dumps(data, ensure_ascii=False) + "</script>")


def _receipt_hero(amount_inr: float, paid_at: str, tag: str) -> str:
    """Payment-receipt hero card — dark panel, the amount front and center,
    paid date and receipt number, like the receipts big billing systems send."""
    return f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_C_NAVY};border-radius:14px;margin:0 0 18px"><tr><td style="padding:24px 26px">
<p style="margin:0 0 6px;color:#9aa3b8;font-size:13px">Receipt from {_BRAND_NAME}</p>
<p style="margin:0;color:#ffffff;font-size:36px;font-weight:bold;line-height:1.1">{_inr(amount_inr)}</p>
<p style="margin:6px 0 0;color:{_C_GREEN};font-size:13px;font-weight:bold">Paid {_html.escape(paid_at)}</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:16px;border-top:1px solid #2a3152"><tr>
<td style="padding-top:12px;color:#9aa3b8;font-size:13px">Receipt number</td>
<td style="padding-top:12px;color:#ffffff;font-size:13px;font-weight:bold" align="right">{_html.escape(tag)}</td>
</tr></table></td></tr></table>"""


def _rates_box(rates: dict[str, float]) -> str:
    """Every live payout method's rate in one box (0-rate methods are hidden)."""
    from .config import SERVICES as _SV
    live = [(k, rates[k]) for k in _SV if k in rates and rates[k] > 0]
    rows = "".join(
        f"<tr><td style='padding:10px 18px;color:{_C_OK};font-size:15px;font-weight:bold'>"
        f"{_html.escape(_SV.get(k, k))}</td>"
        f"<td style='padding:10px 18px;color:{_C_OK};font-size:20px;font-weight:bold' align='right'>"
        f"&#8377;{r:g} <span style='font-size:13px;font-weight:normal'>/ USDT</span></td></tr>"
        for k, r in live)
    return (f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
            f"style='background:{_C_SOFT};border-radius:12px;margin:0 0 18px'>{rows}</table>")


def rate_update_email(rates: dict[str, float]) -> tuple[str, str]:
    """(subject, html) for the auto 'rates changed' blast — every live payout
    method's rate in one box, matching what the site shows right now."""
    from .config import SERVICES as _SV
    live = [(k, rates[k]) for k in _SV if k in rates and rates[k] > 0]
    subject = ("USDT → INR rates updated — "
               + " · ".join(f"{k} ₹{r:g}" for k, r in live)[:120])
    site = (settings.site_url or "").rstrip("/")
    inner = f"""{_badge("&#8377;", bg=_C_NAVY, fg=_C_GREEN)}
<h1 style="margin:0 0 10px;color:{_C_NAVY};font-size:23px;line-height:1.25">Rates just updated</h1>
<p style="margin:0 0 14px;color:{_C_INK};font-size:15px;line-height:1.6">Live USDT&nbsp;&rarr;&nbsp;INR rates on the desk right now:</p>
{_rates_box(rates)}
<p style="margin:0 0 18px;color:{_C_INK};font-size:14px;line-height:1.6">Instant bank payout &mdash; UPI and IMPS land in minutes, every deposit is verified on-chain, and funds are 100% clean. Lock today's rate before it moves:</p>
{_btn(site + "/sell" if site else "#", "Sell USDT now &rarr;")}"""
    return subject, inner


def order_created_email(tag: str, usd: float, service_label: str, rate: float,
                        inr: float, bank_label: str, net_label: str,
                        track_url: str, ttl_min: int) -> tuple[str, str]:
    subject = f"Order {tag} received — sell {_usd(usd)} USDT via {service_label}"
    rows = _kv_rows([
        ("Order", _html.escape(tag)),
        ("Send exactly", f"{_usd(usd)} USDT <span style='font-weight:normal;color:#5a657d'>({_html.escape(net_label)})</span>"),
        ("Payout method", _html.escape(service_label)),
        ("Rate locked", f"&#8377;{rate:g} / USDT"),
        ("You receive", _inr(inr)),
        ("Payout bank", _html.escape(bank_label or "—")),
        ("Deposit window", f"{ttl_min} minutes"),
        ("Payout after confirmation", _html.escape(settings.eta_text)),
    ])
    inner = f"""{_badge("&#10003;")}
<h1 style="margin:0 0 10px;color:{_C_NAVY};font-size:23px;line-height:1.25">Order received &mdash; watching for your deposit</h1>
<p style="margin:0 0 6px;color:{_C_INK};font-size:15px;line-height:1.6">Everything you submitted, in one place:</p>
{rows}
<p style="margin:0 0 16px;color:{_C_INK};font-size:14px;line-height:1.65">
Send the <b>exact amount</b> shown &mdash; the cents are unique to this order, so your
deposit is matched the moment it lands. The deposit address, QR code and a live
timer are on your order page. We'll email you again the second your deposit is received.</p>
{_btn(track_url, "Open your order &rarr;")}
{_ld_order(tag, service_label, inr, "OrderProcessing")}"""
    return subject, inner


def deposit_received_email(tag: str, usd: float, inr: float, service_label: str,
                           bank_label: str, position: int,
                           track_url: str) -> tuple[str, str]:
    """Sent the moment the chain scanner (or an admin confirm) verifies the
    deposit for an order — the middle step between confirmation and receipt."""
    subject = f"Deposit received — {_usd(usd)} USDT credited to order {tag}"
    rows = _kv_rows([
        ("Order", _html.escape(tag)),
        ("Deposit verified", f"<span style='color:{_C_OK}'>{_usd(usd)} USDT</span>"),
        ("Being paid to you", _inr(inr)),
        ("Payout method", _html.escape(service_label)),
        ("Payout bank", _html.escape(bank_label or "—")),
        ("Queue position", f"#{position}"),
        ("Expected payout", _html.escape(settings.eta_text)),
    ])
    inner = f"""{_badge("&#8595;")}
<h1 style="margin:0 0 10px;color:{_C_NAVY};font-size:23px;line-height:1.25">Your deposit is in &mdash; payout on the way</h1>
<p style="margin:0 0 6px;color:{_C_INK};font-size:15px;line-height:1.6">We verified your USDT on-chain for order <b>{_html.escape(tag)}</b>. Nothing more to do &mdash; your bank transfer is now in the payout queue:</p>
{rows}
<p style="margin:0 0 16px;color:{_C_INK};font-size:14px;line-height:1.6">You'll get one more email with the final receipt the moment the bank transfer is sent. Track it live any time:</p>
{_btn(track_url, "Track your payout &rarr;")}
{_ld_order(tag, service_label, inr, "OrderProcessing")}"""
    return subject, inner


def order_cancelled_email(tag: str, usd: float, service_label: str,
                          reason_line: str) -> tuple[str, str]:
    """Cancellation notice — closes the loop on the confirmation email, states
    plainly that no payout will happen, and covers the paid-late edge."""
    subject = f"Order {tag} cancelled"
    rows = _kv_rows([
        ("Order", _html.escape(tag)),
        ("Amount", f"{_usd(usd)} USDT"),
        ("Payout method", _html.escape(service_label)),
        ("Status", "<span style='color:#c0271c'>Cancelled — no payout will be made</span>"),
    ])
    site = (settings.site_url or "").rstrip("/")
    inner = f"""{_badge("&#10005;", bg="#c0271c")}
<h1 style="margin:0 0 10px;color:{_C_NAVY};font-size:23px;line-height:1.25">Order cancelled</h1>
<p style="margin:0 0 6px;color:{_C_INK};font-size:15px;line-height:1.6">{_html.escape(reason_line)}</p>
{rows}
<p style="margin:0 0 16px;color:{_C_INK};font-size:14px;line-height:1.65">
The deposit address from this order is no longer watched for it. <b>If you already
sent USDT</b> for this order, don't worry — open the order page and submit your
transaction ID; the desk verifies it on-chain and settles or refunds you.
Rates stay live around the clock whenever you want to sell:</p>
{_btn(site + "/sell" if site else "#", "Start a new order &rarr;")}
{_ld_order(tag, service_label, 0.0, "OrderCancelled")}"""
    return subject, inner


def claim_submitted_email(tag: str, usd: float, service_label: str,
                          txid: str, track_url: str) -> tuple[str, str]:
    """Sent when a customer's manual TXID passes the automatic on-chain check
    and is queued for the desk's final human approval."""
    subject = f"TXID received — order {tag} under verification"
    short = f"{txid[:10]}…{txid[-6:]}" if len(txid) > 20 else txid
    rows = _kv_rows([
        ("Order", _html.escape(tag)),
        ("Amount", f"{_usd(usd)} USDT"),
        ("Payout method", _html.escape(service_label)),
        ("Transaction", f"<span style='font-family:monospace'>{_html.escape(short)}</span>"),
        ("Status", f"<span style='color:#b45309'>Under manual verification</span>"),
    ])
    inner = f"""{_badge("&#8635;", bg="#b45309")}
<h1 style="margin:0 0 10px;color:{_C_NAVY};font-size:23px;line-height:1.25">Your TXID checks out &mdash; final verification running</h1>
<p style="margin:0 0 6px;color:{_C_INK};font-size:15px;line-height:1.6">Good news: our system matched your transaction on-chain. A desk operator now makes the final confirmation &mdash; this typically takes <b>10&ndash;20 minutes</b>.</p>
{rows}
<p style="margin:0 0 16px;color:{_C_INK};font-size:14px;line-height:1.65">
Nothing more to do. The moment it's approved you'll get the <b>deposit received</b>
email and your bank payout is queued; if it can't be matched you'll hear that too.
Track it live any time:</p>
{_btn(track_url, "Track your order &rarr;")}
{_ld_order(tag, service_label, 0.0, "OrderProblem")}"""
    return subject, inner


def claim_rejected_email(tag: str, usd: float,
                         service_label: str) -> tuple[str, str]:
    """Sent when the desk's manual check can NOT match the claimed TXID to a
    real deposit — closes the claim loop honestly."""
    subject = f"Order {tag} — deposit could not be verified"
    rows = _kv_rows([
        ("Order", _html.escape(tag)),
        ("Amount claimed", f"{_usd(usd)} USDT"),
        ("Payout method", _html.escape(service_label)),
        ("Status", "<span style='color:#c0271c'>Closed — no deposit received</span>"),
    ])
    inner = f"""{_badge("&#10005;", bg="#c0271c")}
<h1 style="margin:0 0 10px;color:{_C_NAVY};font-size:23px;line-height:1.25">We could not verify a deposit for this order</h1>
<p style="margin:0 0 6px;color:{_C_INK};font-size:15px;line-height:1.6">The desk checked the submitted transaction against our address on-chain and no matching USDT deposit was found. The order stays closed and <b>no payout will be made</b>.</p>
{rows}
<p style="margin:0 0 16px;color:{_C_INK};font-size:14px;line-height:1.65">
If you believe this is a mistake, just reply to this email with your full
transaction hash and a screenshot from your wallet — a human will re-check it
personally.</p>
{_ld_order(tag, service_label, 0.0, "OrderCancelled")}"""
    return subject, inner


def order_completed_email(tag: str, usd: float, rate: float, inr: float,
                          service_label: str, bank_label: str,
                          live_rates: dict[str, float] | None = None,
                          paid_at: str = "") -> tuple[str, str]:
    subject = f"{_inr(inr)} paid — order {tag} complete"
    rows = _kv_rows([
        ("Payout method", _html.escape(service_label)),
        ("Payout bank", _html.escape(bank_label or "—")),
        ("USDT received", f"{_usd(usd)} USDT"),
        ("Rate", f"&#8377;{rate:g} / USDT"),
        ("Status", f"<span style='color:{_C_OK}'>Complete &mdash; marked in your account</span>"),
    ])
    site = (settings.site_url or "").rstrip("/")
    rates_push = ""
    if live_rates:
        rates_push = (f"<p style='margin:0 0 8px;color:{_C_INK};font-size:14px;"
                      f"line-height:1.6'><b>Selling more today?</b> These rates are "
                      f"live on the desk right now &mdash; your bank is already "
                      f"saved, so the next order takes under a minute:</p>"
                      + _rates_box(live_rates))
    inner = f"""<h1 style="margin:0 0 12px;color:{_C_NAVY};font-size:23px;line-height:1.25">Payment sent</h1>
{_receipt_hero(inr, paid_at or _ist_now_str(), tag)}
{rows}
{rates_push}
{_btn(site + "/sell" if site else "#", "Sell again &rarr;")}
<p style="margin:16px 0 0;color:#8b95a8;font-size:13px;line-height:1.6">If the credit doesn't show in your bank within a few minutes, reply to this email with your order number and we'll check it immediately.</p>
{_ld_order(tag, service_label, inr, "OrderDelivered")}"""
    return subject, inner


async def account_email_for_uid(uid: int) -> tuple[str, str]:
    """(email, name) when uid belongs to a signed-up website account with a
    VERIFIED email; ('','') otherwise (Telegram ids, anon uids, unverified)."""
    if uid >= 0:
        return "", ""
    acct_id = -uid - _ACCT_BASE
    if acct_id <= 0:
        return "", ""
    async with Session() as s:
        a = await s.get(Account, acct_id)
    if (a and a.email and _EMAIL_RE.match(a.email.strip())
            and getattr(a, "email_verified", False)):
        return a.email.strip(), a.name or ""
    return "", ""


async def email_for_uid(uid: int) -> tuple[str, str]:
    """Verified email for ANY customer: website accounts (negative uids) or
    Telegram users who added one with /email + /verify (positive ids)."""
    if uid < 0:
        return await account_email_for_uid(uid)
    async with Session() as s:
        u = await s.get(User, uid)
    if (u and u.email and u.email_verified and _EMAIL_RE.match(u.email.strip())):
        return u.email.strip(), u.first_name or ""
    return "", ""


# ── email OTP (6-digit) — the front door that keeps fake addresses out ───────
# In-memory per-uid store: {email, code, exp, tries, sends}. A process restart
# clears pending codes, which only means "request a fresh one" — no data loss.

_otps: dict[int, dict] = {}
OTP_TTL = 15 * 60
OTP_MAX_TRIES = 5
OTP_MAX_SENDS_PER_HOUR = 3


def otp_email(code: str) -> tuple[str, str]:
    subject = f"{code} is your {_BRAND_NAME} verification code"
    digits = "".join(
        f"<td style='width:44px;height:56px;background:#f4f7fa;border:1px solid #e6eaf1;"
        f"border-radius:10px;text-align:center;color:{_C_NAVY};font-size:26px;"
        f"font-weight:bold'>{d}</td><td style='width:8px'></td>"
        for d in code)
    inner = f"""<h1 style="margin:0 0 10px;color:{_C_NAVY};font-size:23px;line-height:1.25">Verify your email</h1>
<p style="margin:0 0 16px;color:{_C_INK};font-size:15px;line-height:1.6">Enter this code to confirm this address receives your order updates and payment receipts:</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 18px"><tr>{digits}</tr></table>
<p style="margin:0;color:#8b95a8;font-size:13px;line-height:1.6">The code expires in 15 minutes. If you didn't request it, you can safely ignore this email.</p>"""
    return subject, inner


def _otp_bucket(uid: int) -> dict:
    b = _otps.get(uid)
    if b is None or time.time() > b["exp"]:
        b = {"email": "", "code": "", "exp": 0.0, "tries": 0, "sends": []}
        _otps[uid] = b
    return b


async def issue_email_otp(uid: int, email: str) -> tuple[bool, str]:
    """Generate + email a 6-digit code for this uid/email. Rate-limited."""
    email = (email or "").strip()
    if not _EMAIL_RE.match(email):
        return False, "That doesn't look like a valid email address."
    cfg = await email_config()
    if not email_ready(cfg):
        return False, "Email delivery isn't set up yet — ask support."
    b = _otp_bucket(uid)
    now = time.time()
    b["sends"] = [t for t in b["sends"] if now - t < 3600]
    if len(b["sends"]) >= OTP_MAX_SENDS_PER_HOUR:
        return False, "Too many codes requested — try again in an hour."
    b.update({"email": email, "code": f"{secrets.randbelow(1_000_000):06d}",
              "exp": now + OTP_TTL, "tries": 0})
    b["sends"].append(now)
    subj, inner = otp_email(b["code"])
    await send_transactional(email, "", subj, inner, stream="otp")
    return True, email


def verify_email_otp(uid: int, code: str) -> tuple[bool, str]:
    """(ok, verified_email | error). Constant-shape checks with attempt cap."""
    b = _otps.get(uid)
    if not b or not b["code"] or time.time() > b["exp"]:
        return False, "That code expired — request a fresh one."
    if b["tries"] >= OTP_MAX_TRIES:
        return False, "Too many wrong attempts — request a fresh code."
    b["tries"] += 1
    if not hmac.compare_digest((code or "").strip(), b["code"]):
        return False, "Wrong code — check the email and try again."
    email = b["email"]
    _otps.pop(uid, None)
    return True, email


async def send_transactional(to_addr: str, to_name: str, subject: str,
                             inner_html: str, fail_bot=None,
                             fail_uid: int = 0, stream: str = "tx",
                             legal: bool = False) -> bool:
    """Fire-and-forget single branded email (order confirmations/receipts).
    Never raises into the order flow; returns False when email isn't set up.
    Skips the unsubscribe list by design — these are receipts, not marketing —
    and carries no unsubscribe footer. When fail_bot + a positive fail_uid are
    given and the SMTP relay rejects the address, the Telegram user is told
    their email bounced so they can fix it with /email. `stream` picks the
    From address ('tx' for order mail, 'otp' for verification codes);
    legal=True adds the compliance footer (order lifecycle emails only)."""
    cfg = await email_config()
    if not email_ready(cfg) or not _EMAIL_RE.match((to_addr or "").strip()):
        return False
    cfg = _stream_cfg(cfg, stream)
    tg_handle = ""
    if legal:
        async with Session() as s:
            support = await get_setting(s, "support") or ""
        tg_handle = next((h.lstrip("@") for h in support.split()
                          if h.startswith("@")), "")
    body = brand_wrap(inner_html,
                      contact=cfg.get("reply_to") or cfg["from_addr"],
                      legal=legal, tg_handle=tg_handle)

    async def _run():
        def _noop(_s, _f):
            pass
        failed = 0
        try:
            _sent, failed, _errs = await asyncio.to_thread(
                _send_batch_blocking, cfg, [((to_addr or "").strip(), to_name)],
                subject, body, True, b"", _noop)   # secret unused: no unsub link
        except Exception:
            failed = 1
            log.exception("transactional email to %s failed", to_addr)
        if failed and fail_bot is not None and fail_uid > 0:
            try:
                from .helpers import notify_user
                await notify_user(
                    fail_bot, fail_uid,
                    f"⚠️ We couldn't deliver your order email to "
                    f"<code>{_html.escape(to_addr)}</code> — the address looks "
                    "wrong. Use /email your@address.com to set the correct one "
                    "so you keep receiving order updates.")
            except Exception:
                log.exception("bounce notice to %s failed", fail_uid)

    _spawn(_run())
    return True


async def maybe_rate_blast(bot=None, prev_rates: dict | None = None) -> str:
    """Auto-email subscribers when the live rate set actually changes.

    Dedupe: the last-EMAILED rate snapshot is stored in the Setting
    'rate_email_last'; identical rates never re-send. Anti-spam: at most one
    rate email per RATE_BLAST_GAP — rapid tweaks collapse into one delayed
    email carrying the latest rates (a retry task re-checks when the gap ends).
    First run with no history: prev_rates (captured by the caller BEFORE
    saving) decides whether this was a real change; with neither history nor
    prev_rates, the current rates become the baseline silently."""
    from .db import get_rates
    async with Session() as s:
        if (await get_setting(s, "rate_email_auto") or "1") == "0":
            return "off"
        rates = await get_rates(s)
        last_json = await get_setting(s, "rate_email_last") or ""
    if not rates:
        return "no live rates"
    snap = json.dumps({k: v for k, v in sorted(rates.items())})
    try:
        last = json.loads(last_json) if last_json else {}
    except ValueError:
        last = {}
    if last:
        baseline, ts = last.get("snap", ""), float(last.get("ts", 0))
    elif prev_rates is not None:
        baseline = json.dumps({k: v for k, v in sorted(prev_rates.items())})
        ts = 0.0
        # persist the pre-change baseline NOW — a later retry runs without
        # prev_rates, and without stored history it would record the changed
        # rates as the baseline and silently drop the pending email
        async with Session() as s:
            await set_setting(s, "rate_email_last",
                              json.dumps({"snap": baseline, "ts": 0}))
    else:
        async with Session() as s:   # first sighting — baseline only, no blast
            await set_setting(s, "rate_email_last",
                              json.dumps({"snap": snap, "ts": 0}))
        return "baseline recorded"
    if snap == baseline:
        return "unchanged"
    cfg = await email_config()
    if not email_ready(cfg):
        return "email not configured"   # no retry loop — nothing to send with
    wait = RATE_BLAST_GAP - (time.time() - ts)
    if wait > 0:
        _schedule_rate_retry(wait, bot)
        return f"cooldown — latest rates go out in ~{int(wait // 60) + 1} min"
    # re-check right before claiming the broadcast: another coroutine may have
    # just sent for this exact snapshot while we were reading config above
    async with Session() as s:
        recheck = await get_setting(s, "rate_email_last") or ""
    try:
        if recheck and json.loads(recheck).get("snap") == snap:
            return "unchanged"
    except ValueError:
        pass
    subject, inner = rate_update_email(rates)
    ok, msg = await start_email_broadcast(
        subject, brand_wrap(inner, contact=cfg.get("from_addr", "")), True, bot=bot)
    if ok:
        async with Session() as s:
            await set_setting(s, "rate_email_last",
                              json.dumps({"snap": snap, "ts": time.time()}))
        return "rate email sending"
    if "already running" in msg:
        _schedule_rate_retry(120, bot)   # busy is transient — try again shortly
    return msg   # persistent refusals (no recipients / no site URL): no loop


def spawn_rate_blast(bot=None, prev_rates: dict | None = None) -> None:
    """Fire-and-forget wrapper for the rate-change hooks (panel save and the
    /setrate command) — never blocks or raises into the caller."""
    _spawn(maybe_rate_blast(bot, prev_rates))


def _schedule_rate_retry(delay: float, bot) -> None:
    if _rate_retry["pending"]:
        return
    _rate_retry["pending"] = True

    async def _later():
        try:
            await asyncio.sleep(max(5.0, delay))
            _rate_retry["pending"] = False
            await maybe_rate_blast(bot)
            _rate_retry["attempts"] = 0
        except Exception:
            _rate_retry["pending"] = False
            log.exception("delayed rate blast failed")
            # one transient error must not drop the pending email for good —
            # retry a bounded number of times, then give up loudly (logged)
            _rate_retry["attempts"] = _rate_retry.get("attempts", 0) + 1
            if _rate_retry["attempts"] <= 3:
                _schedule_rate_retry(120, bot)

    _spawn(_later())
