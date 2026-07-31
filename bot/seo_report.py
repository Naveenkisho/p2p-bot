"""Daily SEO report — a morning email to the operator.

Every day at 09:00 IST the desk emails a short, honest status of the onsite
SEO work: how many /learn articles are live, which keywords from the plan are
covered vs still pending, the next keyword to target, and ONE concrete
off-page action for the day (rotating through a fixed list of legitimate,
human-effort backlink tasks — no spam, no automation).

The recipient is set in the panel (Settings → Website → Daily SEO report
email); blank turns the report off. Sends through the same SMTP as customer
mail, and a last-sent-date guard keeps restarts from double-sending.
"""

import asyncio
import html as _html
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import Session, get_setting, set_setting

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
SEND_HOUR_IST = 9              # 09:00 IST every morning
_KEYWORDS_MD = Path(__file__).resolve().parent.parent / "content" / "KEYWORDS.md"

# One honest off-page task per day, rotating. Each is something a human does
# by hand — the kind of link/mention Google actually rewards. No link farms,
# no paid link schemes, no automated posting.
OFFPAGE_ACTIONS = [
    ("Answer one real question", "Find one genuine 'how do I sell USDT in "
     "India' question on Quora or Reddit (r/CryptoIndia) and write a helpful, "
     "complete answer from experience. Link a /learn guide only if the "
     "subreddit/space allows links — the answer must stand on its own."),
    ("Publish the next article", "Take the next pending keyword from the plan "
     "and ship its /learn article (600-900 words, interlinked). Fresh, "
     "genuinely useful content is still the strongest signal."),
    ("Telegram channel post", "Post today's USDT-INR rate insight on the "
     "desk's Telegram channel with a link to the live rates page — real "
     "activity that earns branded searches and direct visits."),
    ("One directory listing", "Submit the site to ONE quality directory "
     "today (a crypto services directory, an India fintech listing, or an "
     "OTC-desk aggregator). One accurate listing beats ten spammy ones."),
    ("Ask for a mention", "Ask one long-standing customer if they'd mention "
     "the desk in a Telegram group or forum they're active in — a real "
     "recommendation in their own words, never a script you hand them."),
    ("Guest-post pitch", "Email ONE Indian crypto/personal-finance blog and "
     "offer a genuinely useful article (e.g. how on-chain USDT settlement "
     "works). One thoughtful pitch a week lands better than mass emails."),
    ("Refresh an old article", "Open the oldest /learn article, update the "
     "rates/examples to today's numbers and add one new internal link. "
     "Freshness on existing pages is cheap and compounding."),
]


def keyword_stats() -> dict:
    """Parse content/KEYWORDS.md — {done, pending, next, total}."""
    done = pending = 0
    next_kw = ""
    try:
        text = _KEYWORDS_MD.read_text(encoding="utf-8")
    except OSError:
        return {"done": 0, "pending": 0, "next": "", "total": 0}
    for line in text.splitlines():
        m = re.match(r"^\|\s*\d+\s*\|\s*([^|]+?)\s*\|\s*(done|pending)\s*\|", line)
        if not m:
            continue
        if m.group(2) == "done":
            done += 1
        else:
            pending += 1
            if not next_kw:
                next_kw = m.group(1)
    return {"done": done, "pending": pending, "next": next_kw,
            "total": done + pending}


async def build_report() -> tuple[str, str]:
    """(subject, inner_html) for today's report."""
    from .config import settings as cfg
    from .website import _articles                # read from disk, no request
    arts = _articles()
    kw = keyword_stats()
    today = datetime.now(IST)
    action_title, action_body = OFFPAGE_ACTIONS[
        today.timetuple().tm_yday % len(OFFPAGE_ACTIONS)]
    site = (cfg.site_url or "").rstrip("/")

    latest = "".join(
        f"<li><a href='{site}/learn/{_html.escape(a['slug'])}'>"
        f"{_html.escape(a['title'])}</a></li>" for a in arts[:3])
    subject = (f"SEO daily — {len(arts)} articles live, "
               f"{kw['pending']} keywords to go")
    nxt = (f"<p style='margin:12px 0 0'><b>Next keyword to target:</b> "
           f"&ldquo;{_html.escape(kw['next'])}&rdquo;</p>" if kw["next"] else
           "<p style='margin:12px 0 0'><b>Keyword plan complete</b> — time to "
           "add the next batch to content/KEYWORDS.md.</p>")
    inner = f"""
<h2 style='margin:0 0 4px'>Daily SEO report</h2>
<p style='margin:0;color:#5a6478'>{today:%A, %d %b %Y} &middot; 09:00 IST</p>
<table role=presentation cellpadding=0 cellspacing=0 style='margin:16px 0;width:100%'>
<tr>
<td style='background:#f2f7f4;border-radius:10px;padding:14px;text-align:center'>
<div style='font-size:1.5rem;font-weight:800'>{len(arts)}</div>
<div style='color:#5a6478;font-size:.85rem'>articles live</div></td>
<td style='width:10px'></td>
<td style='background:#f2f7f4;border-radius:10px;padding:14px;text-align:center'>
<div style='font-size:1.5rem;font-weight:800'>{kw['done']}/{kw['total']}</div>
<div style='color:#5a6478;font-size:.85rem'>keywords covered</div></td>
<td style='width:10px'></td>
<td style='background:#f2f7f4;border-radius:10px;padding:14px;text-align:center'>
<div style='font-size:1.5rem;font-weight:800'>{kw['pending']}</div>
<div style='color:#5a6478;font-size:.85rem'>keywords pending</div></td>
</tr></table>
{nxt}
<div style='background:#eef4ff;border-radius:10px;padding:14px;margin:16px 0'>
<b>Today's off-page action — {_html.escape(action_title)}</b>
<p style='margin:6px 0 0;color:#333'>{_html.escape(action_body)}</p></div>
<p style='margin:12px 0 4px'><b>Latest articles</b></p>
<ul style='margin:0;padding-left:20px'>{latest or '<li>none yet</li>'}</ul>
<p style='margin:14px 0 0;color:#5a6478;font-size:.85rem'>
Checklist: sitemap at {site or 'your site'}/sitemap.xml is submitted in Google
Search Console &middot; every new article is interlinked &middot; content ships
by git pull, no restart needed.</p>"""
    return subject, inner


async def run_report(force: bool = False) -> bool:
    """Send today's report once. Returns True when an email actually went."""
    from . import sender
    async with Session() as s:
        to_addr = (await get_setting(s, "seo_report_email") or "").strip()
        last = await get_setting(s, "seo_report_last") or ""
    today_key = datetime.now(IST).strftime("%Y-%m-%d")
    if not to_addr:
        return False
    if last == today_key and not force:
        return False
    subject, inner = await build_report()
    # plain wrap: the standard brand footer, no OTP/anti-phishing line and no
    # customer compliance block — this is an internal operator report
    ok = await sender.send_transactional(to_addr, "", subject, inner,
                                         stream="mkt")
    if ok:
        async with Session() as s:
            await set_setting(s, "seo_report_last", today_key)
            await s.commit()
        log.info("daily SEO report sent to %s", to_addr)
    return ok


def _secs_to_next_send() -> float:
    now = datetime.now(IST)
    target = now.replace(hour=SEND_HOUR_IST, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def seo_report_loop() -> None:
    """Background loop: send at 09:00 IST daily. Never crashes the app."""
    while True:
        try:
            await asyncio.sleep(_secs_to_next_send())
            await run_report()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("seo report loop error")
            await asyncio.sleep(3600)     # don't spin on a persistent failure
