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
import html as _html
import json
import logging
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from urllib.parse import quote

import aiohttp
from sqlalchemy import select

from .config import settings
from .db import Session, get_setting, site_secret
from .helpers import unsub_token
from .models import Account, Unsubscribe

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
        }
    return cfg


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
    """(email, name) for every website account with a valid email, minus any
    that unsubscribed. De-duplicated case-insensitively."""
    async with Session() as s:
        accounts = (await s.scalars(select(Account))).all()
        unsub = {e.lower() for e in
                 (await s.scalars(select(Unsubscribe.email))).all()}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in accounts:
        raw = (a.email or "").strip()
        low = raw.lower()
        if raw and _EMAIL_RE.match(raw) and low not in seen and low not in unsub:
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
    Returns (sent, failed, first_errors)."""
    sent = failed = 0
    errors: list[str] = []
    srv = _smtp_connect(cfg)
    try:
        for to_addr, to_name in recipients:
            try:
                url = _unsub_url(to_addr, secret)
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
    cfg = await email_config()
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
