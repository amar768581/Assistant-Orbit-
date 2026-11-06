"""
security_policy.py — ORBIT's non-bypassable financial & secret guardrail.

One source of truth so every layer agrees on what "banking" means:

    from security_policy import is_financial_target, contains_secret

    is_financial_target("SBI YONO")        # True
    is_financial_target("com.phonepe.app") # True
    contains_secret("your OTP is 445566")  # True

Enforced in four places (each re-exports these names):

1. ``adb_automation.open_app`` — never launches a banking/UPI/wallet/trading app.
2. ``adb_automation.read_screen`` — refuses if the foreground window is financial
   or ``FLAG_SECURE`` (fail-closed when the window cannot be inspected).
3. ``brain.validate_decision`` — a model request to open a bank app or send a
   PIN/OTP/password becomes a Bengali ``speak`` refusal instead.
4. ``notification_responder`` / ``memory_engine`` — OTPs, PINs, CVVs, passwords
   and bank transaction alerts are never logged, stored, or auto-replied to.

The list below is intentionally broad. Blocking a non-financial app is a minor
annoyance; letting ORBIT touch a bank is unacceptable.
"""

from __future__ import annotations

import re
from typing import Any, Dict, FrozenSet, Iterable, Tuple

# --------------------------------------------------------------------------- #
# Refusals (always addressed to Boss)
# --------------------------------------------------------------------------- #

BANKING_REFUSAL_BN = (
    "বস, নিরাপত্তার কারণে আমি কোনো ব্যাংকিং বা পেমেন্ট অ্যাপ খুলি না বা লগইন করি না।"
)
SECRET_REFUSAL_BN = (
    "বস, নিরাপত্তার কারণে পিন, ওটিপি বা পাসওয়ার্ড আমি পড়ি না, লিখি না বা পাঠাই না।"
)
SCREEN_REFUSAL_BN = (
    "বস, নিরাপত্তার কারণে এই স্ক্রিনের স্ক্রিনশট নিচ্ছি না — "
    "ব্যাংকিং অ্যাপ বা সুরক্ষিত (FLAG_SECURE) উইন্ডো খোলা আছে।"
)
NOTIFICATION_REFUSAL_BN = (
    "বস, ব্যাংকিং বা OTP সংক্রান্ত নোটিফিকেশন আমি পড়ি না, সংরক্ষণ করি না, উত্তরও দিই না।"
)
SENSITIVE_SKIP_BN = "বস, ওটিপি/পিন/পাসওয়ার্ড যুক্ত কথা আমি মনে রাখি না।"

# --------------------------------------------------------------------------- #
# Apps & packages
# --------------------------------------------------------------------------- #

BLOCKED_FINANCIAL_APPS: Dict[str, Tuple[str, ...]] = {
    # --- Indian banks -----------------------------------------------------
    "sbi yono": ("com.sbi.lotusintouch", "com.sbi.SBIFreedomPlus", "com.sbi.upi"),
    "hdfc bank": ("com.hdfc.retailbanking", "com.hdfc.mobilebanking", "com.hdfc.upi"),
    "icici bank": ("com.icicibank.mobilebanking", "com.icicibank.pockets", "com.icicibank.imobile"),
    "axis bank": ("com.axis.mobile", "com.axis.bank"),
    "kotak bank": ("com.kotak.mobileapp", "com.kotak.bank"),
    "pnb": ("com.pnb.netpnb", "com.pnb.mobilebanking"),
    "bank of baroda": ("com.bankofbaroda.mconnect",),
    "canara bank": ("com.canara.bank",),
    "union bank": ("com.unionbank.mobilebanking",),
    "idfc first": ("com.idfcfirst.mobilebanking",),
    "federal bank": ("com.federalbank.mobilebanking",),
    # --- UPI / wallets ----------------------------------------------------
    "google pay": ("com.google.android.apps.nbu.paisa.user",),
    "phonepe": ("com.phonepe.app",),
    "paytm": ("net.one97.paytm", "com.paytm.bank"),
    "bhim": ("in.org.npci.upiapp",),
    "amazon pay": ("in.amazon.mShop.android.shopping",),
    "cred": ("com.dreamplug.androidapp",),
    "mobikwik": ("com.mobikwik_new",),
    "freecharge": ("com.freecharge.android",),
    "slice": ("com.slicepay.slice",),
    "paypal": ("com.paypal.android.p2pmobile",),
    "wise": ("com.transferwise.android",),
    # --- Trading / demat --------------------------------------------------
    "angel one": ("com.msf.angelone", "com.angelbroking.smartapp"),
    "zerodha kite": ("com.zerodhatech.kite",),
    "groww": ("com.nextbillion.groww",),
    "upstox": ("com.upstox.android",),
}

BLOCKED_PACKAGES: FrozenSet[str] = frozenset(
    package for packages in BLOCKED_FINANCIAL_APPS.values() for package in packages
)
_BLOCKED_PACKAGES_LOWER: FrozenSet[str] = frozenset(p.lower() for p in BLOCKED_PACKAGES)

# Substrings that mark a package as financial even if it is not in the table.
_PACKAGE_KEYWORDS: Tuple[str, ...] = (
    "bank", "banking", "upi", "wallet", "netbanking", "net_banking",
    "finance", "trading", "broking", "broker", "demat", "paytm", "phonepe",
    "paisa", "paypal", "mobikwik", "freecharge", "slicepay", "upstox",
    "groww", "zerodha", "angelone", "creditcard", "loan",
)

# Substrings for spoken app names (spaces/punctuation removed).
_NAME_KEYWORDS: Tuple[str, ...] = (
    "bank", "banking", "yono", "upi", "pay", "wallet", "netbanking",
    "finance", "financial", "trading", "trade", "demat", "broking", "broker",
    "credit", "loan", "angelone", "zerodha", "kite", "groww", "paytm",
    "phonepe", "bhim", "cred", "mobikwik", "freecharge", "slice", "upstox",
    "paisa", "sbi", "hdfc", "icici", "kotak", "pnb", "idfc", "canara",
    "ব্যাংক", "পেমেন্ট", "ওয়ালেট", "লেনদেন", "শেয়ার", "স্টক", "টাকা",
)

# --------------------------------------------------------------------------- #
# Secret / bank-alert text
# --------------------------------------------------------------------------- #

_SECRET_SOURCES: Tuple[str, ...] = (
    r"\botp\b",
    r"\bo\.?t\.?p\b",
    r"one[\s\-_]*time[\s\-_]*(?:password|passcode|code|pin)",
    r"verification\s+(?:code|number|pin)",
    r"security\s+code",
    r"\bcvv\b",
    r"\bcvc\b",
    r"\bpin\b",
    r"\bpassword\b",
    r"\bpasscode\b",
    r"\batm\s+pin\b",
    r"পিন(?:\s*(?:নম্বর|কোড))?",
    r"ও\s*টি\s*পি",
    r"ওটিপি",
    r"পাস\s*ওয়ার্ড",
    r"পাসওয়ার্ড",
    r"পাস\s*কোড",
    r"গোপন\s*(?:কোড|নম্বর)",
    r"ভেরিফিকেশন\s*(?:কোড|নম্বর)",
    r"সিভিভি",
)

_BANK_ALERT_SOURCES: Tuple[str, ...] = (
    r"\bdebited\b",
    r"\bcredited\b",
    r"\bwithdrawn\b",
    r"\bwithdrawal\b",
    r"\ba/c\b",
    r"\bacct\b",
    r"\bavl\s+bal\b",
    r"\bavailable\s+balance\b",
    r"\bneft\b",
    r"\bimps\b",
    r"\bupi\b",
    r"\bnetbanking\b",
    r"\bifsc\b",
    r"\bcredit\s+card\b",
    r"\bmin(?:imum)?\s+due\b",
    r"ব্যাংক",
    r"ডেবিট",
    r"ক্রেডিট",
    r"লেনদেন",
    r"ব্যালেন্স",
    r"অ্যাকাউন্টে",
    r"টাকা\s*(?:কাটা|জমা|কাটল|জমা)",
    r"উত্তোলন",
    r"এটিএম",
)

_SECRET_RE = re.compile("|".join(_SECRET_SOURCES), re.IGNORECASE | re.UNICODE)
_BANK_ALERT_RE = re.compile("|".join(_BANK_ALERT_SOURCES), re.IGNORECASE | re.UNICODE)

# Words that turn a generic request into a financial transaction/login attempt.
_FINANCIAL_CONTEXT: Tuple[str, ...] = (
    "bank", "ব্যাংক", "upi", "account", "অ্যাকাউন্ট", "payment", "পেমেন্ট",
    "wallet", "ওয়ালেট", "trading", "ট্রেডিং", "stock", "শেয়ার", "loan",
    "credit card", "কার্ড", "balance", "ব্যালেন্স", "net banking",
)
_FINANCIAL_ACTIONS: Tuple[str, ...] = (
    "login", "log in", "sign in", "লগইন", "transfer", "send money", "টাকা",
    "withdraw", "উত্তোলন", "balance", "ব্যালেন্স", "payment", "পেমেন্ট",
    "otp", "pin", "password", "পিন", "ওটিপি", "পাসওয়ার্ড", "কিনব", "কিনতে",
)

# Focused-window parsing for `dumpsys window`.
_FOCUS_PATTERNS = (
    re.compile(r"mCurrentFocus=Window\{[^}]*?\s([A-Za-z][\w]*(?:\.[\w]+)+)/"),
    re.compile(r"mFocusedApp=.*?\s([A-Za-z][\w]*(?:\.[\w]+)+)/"),
)


def _compact(value: Any) -> str:
    return re.sub(r"[^\w\u0980-\u09FF]+", "", str(value or "").lower())


def is_financial_package(package: Any) -> bool:
    """True for any bank/UPI/wallet/trading package (exact or keyword)."""
    value = str(package or "").strip().lower()
    if not value:
        return False
    if value in _BLOCKED_PACKAGES_LOWER:
        return True
    if "pay" in value:  # Google Pay, Paytm, PayPal, payment.*, in.amazon...paisa is below
        return True
    return any(keyword in value for keyword in _PACKAGE_KEYWORDS)


def is_financial_app_name(name: Any) -> bool:
    """True for spoken names like "SBI YONO", "PhonePe", "ব্যাংক অ্যাপ"."""
    compact = _compact(name)
    if not compact:
        return False
    return any(_compact(keyword) in compact for keyword in _NAME_KEYWORDS)


def is_financial_target(value: Any) -> bool:
    """True for either a package id or a human app name."""
    text = str(value or "").strip()
    if not text:
        return False
    return is_financial_package(text) or is_financial_app_name(text)


def financial_app_block(target: Any) -> Dict[str, Any]:
    """The exact refusal dict returned by ``open_app`` for a blocked app."""
    return {
        "ok": False,
        "action": "open_app",
        "error": "banking_app_blocked",
        "blocked": True,
        "app": str(target or ""),
        "reply_bn": BANKING_REFUSAL_BN,
        "message_bn": BANKING_REFUSAL_BN,
    }


def contains_secret(text: Any) -> bool:
    """OTP, PIN, CVV, password or verification code in any language we serve."""
    return bool(_SECRET_RE.search(str(text or "")))


def is_bank_alert(text: Any) -> bool:
    """Debit/credit/UPI/balance style bank notification."""
    return bool(_BANK_ALERT_RE.search(str(text or "")))


def is_sensitive_text(text: Any) -> bool:
    return contains_secret(text) or is_bank_alert(text)


def is_sensitive_notification(item: Dict[str, Any]) -> bool:
    """Financial package, or any secret/bank-alert wording in the payload."""
    if not isinstance(item, dict):
        return False
    if is_financial_package(item.get("package") or item.get("packageName")):
        return True
    blob = " ".join(
        str(item.get(key) or "")
        for key in ("title", "content", "text", "bigText", "subText", "app")
    )
    return is_sensitive_text(blob)


def is_financial_request(text: Any) -> bool:
    """A request to log in to, transfer from, or check a financial account."""
    value = str(text or "").strip().lower()
    if not value:
        return False
    if contains_secret(value):
        return True
    if is_financial_target(value):
        return True
    has_context = any(word in value for word in _FINANCIAL_CONTEXT)
    has_action = any(word in value for word in _FINANCIAL_ACTIONS)
    return has_context and has_action


def redact_sensitive(text: Any) -> str:
    """For defensive logging: never print the secret itself."""
    value = str(text or "")
    if not value:
        return ""
    redacted = _SECRET_RE.sub("[redacted]", value)
    if redacted != value:
        # Drop the code that usually follows "OTP/PIN is …".
        redacted = re.sub(r"\b\d{4,8}\b", "[redacted]", redacted)
    return redacted


# --------------------------------------------------------------------------- #
# Screen guard
# --------------------------------------------------------------------------- #

def parse_foreground_window(dumpsys_text: str) -> Dict[str, Any]:
    """Extract the focused package and FLAG_SECURE from ``dumpsys window``."""
    text = str(dumpsys_text or "")
    package = ""
    for pattern in _FOCUS_PATTERNS:
        match = pattern.search(text)
        if match:
            package = match.group(1)
            break
    secure = "FLAG_SECURE" in text or "mSecure=true" in text
    financial = is_financial_package(package)
    reason = None
    if secure:
        reason = "flag_secure"
    elif financial:
        reason = "financial_app"
    return {
        "ok": bool(package),
        "package": package or None,
        "secure": secure,
        "financial": financial,
        "blocked": bool(secure or financial),
        "reason": reason,
    }


def evaluate_screen(dumpsys_text: str, unavailable: bool = False) -> Dict[str, Any]:
    """Fail-closed screen decision: no readable window => no screenshot."""
    if unavailable or not str(dumpsys_text or "").strip():
        return {
            "ok": False,
            "package": None,
            "secure": False,
            "financial": False,
            "blocked": True,
            "reason": "guard_unavailable",
            "reply_bn": SCREEN_REFUSAL_BN,
        }
    info = parse_foreground_window(dumpsys_text)
    if not info["package"] and not info["secure"]:
        info.update(
            {
                "ok": False,
                "blocked": True,
                "reason": "guard_unavailable",
                "reply_bn": SCREEN_REFUSAL_BN,
            }
        )
        return info
    if info["blocked"]:
        info["reply_bn"] = SCREEN_REFUSAL_BN
    return info


def security_report() -> Dict[str, Any]:
    """Summary used by ``/status`` and ``main.py doctor``."""
    return {
        "ok": True,
        "enabled": True,
        "apps": len(BLOCKED_FINANCIAL_APPS),
        "packages": len(BLOCKED_PACKAGES),
        "screen_guard": True,
        "secret_guard": True,
        "label_bn": (
            f"চালু — {len(BLOCKED_FINANCIAL_APPS)}টি অ্যাপ / {len(BLOCKED_PACKAGES)}টি প্যাকেজ ব্লকড, "
            "OTP ও স্ক্রিন সুরক্ষা সক্রিয়"
        ),
    }


def blocked_app_names() -> Iterable[str]:
    return BLOCKED_FINANCIAL_APPS.keys()
