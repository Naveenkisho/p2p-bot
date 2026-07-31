"""Payout receipt as a PDF — attached to the payment-done email.

Pure Python, zero dependencies: the PDF is assembled from raw objects with a
programmatically computed xref, so deploying stays `git pull` — no pip step on
the server. One clean A4 page: brand header, receipt number/date, the trade
rows, the on-chain TXID, and a computer-generated note. Standard Helvetica /
Courier only (WinAnsi has no rupee glyph, so amounts say "INR")."""

from datetime import datetime

_W, _H = 595, 842               # A4 in points
_MARGIN = 56
_NAVY = "0.055 0.075 0.188"     # #0e1330
_GREEN = "0.0 0.761 0.435"      # #00c26f
_GREY = "0.42 0.45 0.52"
_INK = "0.10 0.12 0.18"


# common typographic characters that DO exist in WinAnsi — emitted as octal
# escapes so dashes and bullets render instead of degrading to "?"
_WINANSI = {"\u2014": "\\227", "\u2013": "\\226", "\u2022": "\\225",
            "\u2018": "\\221", "\u2019": "\\222", "\u201c": "\\223",
            "\u201d": "\\224", "\u2026": "\\205", "\u20b9": "Rs "}


def _esc(s: str) -> str:
    """PDF literal-string escaping + WinAnsi-safe fallback."""
    out = []
    for ch in str(s):
        if ch in _WINANSI:
            out.append(_WINANSI[ch])
        elif ch in "()\\":
            out.append("\\" + ch)
        elif 32 <= ord(ch) < 127 or 160 <= ord(ch) <= 255:
            out.append(ch)
        else:
            out.append("?")
    return "".join(out)


class _Page:
    def __init__(self):
        self.ops: list[str] = []

    def text(self, x: float, y: float, s: str, size: float = 10,
             bold: bool = False, mono: bool = False, color: str = _INK):
        font = "/F3" if mono else ("/F2" if bold else "/F1")
        self.ops.append(
            f"BT {color} rg {font} {size:g} Tf {x:g} {_H - y:g} Td "
            f"({_esc(s)}) Tj ET")

    def rect(self, x: float, y: float, w: float, h: float, color: str):
        self.ops.append(f"{color} rg {x:g} {_H - y - h:g} {w:g} {h:g} re f")

    def line(self, x1: float, y: float, x2: float, color: str = "0.88 0.90 0.93",
             width: float = 1):
        self.ops.append(f"{color} RG {width:g} w {x1:g} {_H - y:g} m "
                        f"{x2:g} {_H - y:g} l S")


def _assemble(page: _Page) -> bytes:
    content = ("\n".join(page.ops)).encode("latin-1", "replace")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_W} {_H}] "
         "/Resources << /Font << /F1 5 0 R /F2 6 0 R /F3 7 0 R >> >> "
         "/Contents 4 0 R >>").encode(),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
        + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
        b"/Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier "
        b"/Encoding /WinAnsiEncoding >>",
    ]
    buf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(buf))
        buf += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_at = len(buf)
    buf += f"xref\n0 {len(objs) + 1}\n".encode()
    buf += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        buf += f"{off:010d} 00000 n \n".encode()
    buf += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode()
    return bytes(buf)


def receipt_pdf(tag: str, customer: str, usd: float, rate: float, inr: float,
                service: str, bank_label: str, network: str, txid: str,
                paid_at: str, brand: str = "IndiaXchange",
                site: str = "", support_email: str = "") -> bytes:
    """One-page payout receipt. All inputs are plain strings/numbers straight
    from the completed order — nothing is fetched here."""
    p = _Page()
    # brand band
    p.rect(0, 0, _W, 92, _NAVY)
    p.text(_MARGIN, 40, brand, size=20, bold=True, color="1 1 1")
    p.text(_MARGIN, 60, "USDT to INR trading desk", size=9,
           color="0.62 0.67 0.78")
    p.text(_W - _MARGIN - 128, 40, "PAYMENT RECEIPT", size=12, bold=True,
           color=_GREEN)
    p.text(_W - _MARGIN - 128, 58, "PAID IN FULL", size=9, bold=True,
           color="1 1 1")

    y = 132
    p.text(_MARGIN, y, f"Receipt No: {tag}", size=11, bold=True)
    p.text(_W - _MARGIN - 200, y, f"Date: {paid_at}", size=10, color=_GREY)
    y += 18
    if customer:
        p.text(_MARGIN, y, f"Customer: {customer}", size=10, color=_GREY)
        y += 16
    y += 10
    p.line(_MARGIN, y, _W - _MARGIN)
    y += 26

    rows = [
        ("USDT received", f"{usd:g} USDT ({network})"),
        ("Rate locked", f"INR {rate:g} per USDT"),
        ("INR paid out", f"INR {inr:,.2f}"),
        ("Payout method", service),
        ("Payout bank", bank_label or "-"),
    ]
    for label, value in rows:
        p.text(_MARGIN, y, label, size=10, color=_GREY)
        p.text(_MARGIN + 170, y, value, size=11,
               bold=(label == "INR paid out"))
        y += 24

    y += 4
    p.line(_MARGIN, y, _W - _MARGIN)
    y += 24
    p.text(_MARGIN, y, "On-chain deposit (verified)", size=10, color=_GREY)
    y += 16
    p.text(_MARGIN, y, txid or "-", size=8, mono=True)
    y += 30

    p.rect(_MARGIN, y, _W - 2 * _MARGIN, 44, "0.949 0.975 0.961")
    p.text(_MARGIN + 14, y + 18, "Amount paid", size=9, color=_GREY)
    p.text(_MARGIN + 14, y + 34, f"INR {inr:,.2f}", size=15, bold=True)
    p.text(_W - _MARGIN - 130, y + 27, "Status: PAID", size=11, bold=True,
           color="0.05 0.56 0.34")

    fy = _H - 96
    p.line(_MARGIN, fy, _W - _MARGIN)
    contact = " - ".join(x for x in (site, support_email) if x)
    p.text(_MARGIN, fy + 20, contact or brand, size=9, color=_GREY)
    p.text(_MARGIN, fy + 34,
           "Computer-generated receipt - no signature required. "
           "Keep this for your records.", size=8, color=_GREY)
    p.text(_MARGIN, fy + 48,
           "If a payout is delayed or reversed for a reason outside the trade, "
           "the order is re-checked and settled or fully refunded in 3-7 "
           "working days.", size=8, color=_GREY)
    return _assemble(p)
