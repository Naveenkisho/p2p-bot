"""Indian bank directory + IFSC validation for payout bank entry.

An IFSC is 11 chars: 4-letter bank code + '0' + 6-char branch. Picking the bank
from a fixed list and checking the IFSC's first 4 chars against that bank's
code catches the most common payout mistake — a right-looking IFSC for the
wrong bank — before an order is ever created.
"""

import re

# (display name, IFSC 4-letter bank code). Ordered by how common they are for
# retail payouts. 'Other bank' lets anyone with a valid IFSC still transact.
BANKS: list[tuple[str, str]] = [
    ("State Bank of India", "SBIN"),
    ("HDFC Bank", "HDFC"),
    ("ICICI Bank", "ICIC"),
    ("Axis Bank", "UTIB"),
    ("Kotak Mahindra Bank", "KKBK"),
    ("Punjab National Bank", "PUNB"),
    ("Bank of Baroda", "BARB"),
    ("Canara Bank", "CNRB"),
    ("Union Bank of India", "UBIN"),
    ("Bank of India", "BKID"),
    ("IndusInd Bank", "INDB"),
    ("IDFC FIRST Bank", "IDFB"),
    ("Yes Bank", "YESB"),
    ("IDBI Bank", "IBKL"),
    ("Federal Bank", "FDRL"),
    ("Central Bank of India", "CBIN"),
    ("Indian Bank", "IDIB"),
    ("Indian Overseas Bank", "IOBA"),
    ("UCO Bank", "UCBA"),
    ("Bank of Maharashtra", "MAHB"),
    ("Punjab & Sind Bank", "PSIB"),
    ("RBL Bank", "RATN"),
    ("Bandhan Bank", "BDBL"),
    ("AU Small Finance Bank", "AUBL"),
    ("Karur Vysya Bank", "KVBL"),
    ("South Indian Bank", "SIBL"),
    ("Karnataka Bank", "KARB"),
    ("City Union Bank", "CIUB"),
    ("Tamilnad Mercantile Bank", "TMBL"),
    ("Jammu & Kashmir Bank", "JAKA"),
    ("DCB Bank", "DCBL"),
    ("CSB Bank", "CSBK"),
    ("Dhanlaxmi Bank", "DLXB"),
    ("Equitas Small Finance Bank", "ESFB"),
    ("Ujjivan Small Finance Bank", "UJVN"),
    ("Jana Small Finance Bank", "JSFB"),
    ("Suryoday Small Finance Bank", "SURY"),
    ("Utkarsh Small Finance Bank", "UTKS"),
    ("ESAF Small Finance Bank", "ESMF"),
    ("Paytm Payments Bank", "PYTM"),
    ("Airtel Payments Bank", "AIRP"),
    ("India Post Payments Bank", "IPOS"),
    ("Fino Payments Bank", "FINO"),
    ("Standard Chartered Bank", "SCBL"),
    ("HSBC India", "HSBC"),
    ("Citibank India", "CITI"),
    ("DBS Bank India", "DBSS"),
]

OTHER_BANK = "Other bank (not listed)"
ACCOUNT_TYPES = ["Savings", "Current"]

_CODE = {name: code for name, code in BANKS}
# a bank code may map to several display names in theory; here it's 1:1
_NAME_BY_CODE = {code: name for name, code in BANKS}
_IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


def bank_names() -> list[str]:
    return [n for n, _ in BANKS] + [OTHER_BANK]


def is_bank(name: str) -> bool:
    return name in _CODE or name == OTHER_BANK


def ifsc_code_for(bank: str) -> str:
    """The expected IFSC 4-letter prefix for a listed bank ('' for Other)."""
    return _CODE.get(bank, "")


def norm_ifsc(ifsc: str) -> str:
    return re.sub(r"\s", "", (ifsc or "")).upper()


def ifsc_error(bank: str, ifsc: str) -> str:
    """'' when the IFSC is a valid format AND matches the selected bank;
    otherwise a specific, customer-facing error message."""
    code = norm_ifsc(ifsc)
    if not _IFSC_RE.match(code):
        return ("That IFSC doesn't look right — it should be 11 characters like "
                "HDFC0001234 (4 letters, a 0, then 6).")
    if bank == OTHER_BANK or bank not in _CODE:
        return ""
    if code[:4] != _CODE[bank]:
        listed = _NAME_BY_CODE.get(code[:4])
        hint = (f" — that IFSC belongs to {listed}." if listed
                else f" — {bank} IFSCs start with {_CODE[bank]}0.")
        return (f"That IFSC doesn't match {bank}{hint} Pick the right bank or "
                "check the IFSC.")
    return ""


def acct_type_ok(t: str) -> bool:
    return t in ACCOUNT_TYPES
