"""
adb_automation.py — ORBIT's hands on the screen (ADB + Gemini Vision).

Three jobs, one injectable runner (``run_command`` by default) so every path is
testable on a desktop with no phone attached:

    open_app("ইউটিউব")                 # -> monkey / am start
    send_whatsapp(contact="রহিম", message="আসছি")
                                       # -> https://api.whatsapp.com/send?phone=...
    read_screen()                      # -> screencap + Gemini 2.5 Flash vision

    from adb_automation import AdbAutomation
    adb = AdbAutomation()
    adb.open_app("settings")
    adb.send_whatsapp(number="01712345678", message="পৌঁছে গেছি")
    adb.read_screen()["description_bn"]

Everything returns ``{"ok": bool, ...}`` and never raises. Missing ``adb``,
a blocked intent, an absent API key or a broken screenshot all become an error
dict with a Bengali message — the dispatcher can speak it and move on.

Phone setup once (Termux):

    pkg install android-tools          # adb
    # Android 11+: Developer options -> Wireless debugging -> pair
    adb pair 127.0.0.1:37421           # then adb connect 127.0.0.1:<port>
    adb devices                        # must list the phone
"""

from __future__ import annotations

import base64
import json
import logging
import re
import tempfile
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

from brain import GEMINI_MODEL, GEMINI_URL, ensure_boss_address
from config import settings as default_settings
from security_policy import (
    BANKING_REFUSAL_BN,
    BLOCKED_FINANCIAL_APPS,
    BLOCKED_PACKAGES,
    SCREEN_REFUSAL_BN,
    SECRET_REFUSAL_BN,
    contains_secret,
    evaluate_screen,
    financial_app_block,
    is_financial_target,
)
from system_control import CommandResult, SystemControl, normalise_number, run_command

try:  # requests is optional at import time so a bare interpreter still loads.
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

log = logging.getLogger("orbit.adb")

DEFAULT_ADB = ("adb", "shell")
ADB_TIMEOUT = 25.0
VISION_TIMEOUT = 60.0
SCREEN_DIR = Path.home() / ".orbit" / "screens"
MIN_PNG_BYTES = 1024
WHATSAPP_URL = "https://api.whatsapp.com/send?phone={phone}{text}"
VISION_PROMPT_BN = "স্ক্রিনে কী আছে সংক্ষেপে বলো"
VISION_INSTRUCTION = (
    "You are ORBIT, a Bengali assistant looking at the user's Android screen. "
    "Describe what is on the screen in 2-4 short natural Bengali sentences: the app, "
    "the important text/numbers, and anything the user should notice (a message, an "
    "error, a price). Be concrete, never invent text that is not visible. "
    "Address the user as \"বস\"."
)

# --------------------------------------------------------------------------- #
# App table: canonical key -> how to launch it on Android
# --------------------------------------------------------------------------- #

APPS: Dict[str, Dict[str, Any]] = {
    "youtube": {
        "package": "com.google.android.youtube",
        "activity": "com.google.android.youtube.HomeActivity",
        "name_bn": "ইউটিউব",
    },
    "whatsapp": {
        "package": "com.whatsapp",
        "activity": "com.whatsapp.HomeActivity",
        "name_bn": "হোয়াটসঅ্যাপ",
    },
    "facebook": {
        "package": "com.facebook.katana",
        "activity": "com.facebook.katana.LoginActivity",
        "name_bn": "ফেসবুক",
    },
    "telegram": {
        "package": "org.telegram.messenger",
        "activity": "org.telegram.ui.LaunchActivity",
        "name_bn": "টেলিগ্রাম",
    },
    "chrome": {
        "package": "com.android.chrome",
        "activity": "com.google.android.apps.chrome.Main",
        "name_bn": "ক্রোম",
    },
    "camera": {
        "package": "com.google.android.GoogleCamera",
        "activity": "com.android.camera.CameraActivity",
        "alternatives": ["com.android.camera2", "com.sec.android.app.camera", "com.oneplus.camera"],
        "intent": ("android.media.action.IMAGE_CAPTURE", None),
        "name_bn": "ক্যামেরা",
    },
    "settings": {
        "package": "com.android.settings",
        "activity": "com.android.settings.Settings",
        "name_bn": "সেটিংস",
    },
    "clock": {
        "package": "com.google.android.deskclock",
        "activity": "com.android.deskclock.DeskClock",
        "alternatives": ["com.android.deskclock", "com.sec.android.app.clockpackage"],
        "intent": ("android.intent.action.SHOW_ALARMS", None),
        "name_bn": "ঘড়ি",
    },
    "angelone": {
        "package": "com.msf.angelone",
        "activity": "com.msf.angelone.MainActivity",
        "name_bn": "অ্যাঞ্জেল ওয়ান",
    },
}

# Bengali / Banglish / English aliases (normalised: lowercase, spaces removed).
APP_ALIASES: Dict[str, str] = {
    # YouTube
    "youtube": "youtube", "yt": "youtube", "ইউটিউব": "youtube", "ইউটিউবঅ্যাপ": "youtube",
    "ইউটিউবে": "youtube",
    # WhatsApp
    "whatsapp": "whatsapp", "whatsappmessenger": "whatsapp", "wa": "whatsapp",
    "হোয়াটসঅ্যাপ": "whatsapp", "হোয়াটসএপ": "whatsapp", "হোয়াটসঅ্যাপে": "whatsapp",
    "واتساب": "whatsapp",
    # Facebook
    "facebook": "facebook", "fb": "facebook", "ফেসবুক": "facebook",
    # Telegram
    "telegram": "telegram", "tg": "telegram", "টেলিগ্রাম": "telegram",
    # Chrome
    "chrome": "chrome", "browser": "chrome", "গুগলক্রোম": "chrome", "ক্রোম": "chrome",
    "ব্রাউজার": "chrome",
    # Camera
    "camera": "camera", "ক্যামেরা": "camera", "ক্যামেরায়": "camera",
    # Settings
    "settings": "settings", "setting": "settings", "সেটিংস": "settings",
    "সেটিং": "settings",
    # Clock / alarm
    "clock": "clock", "alarm": "clock", "ঘড়ি": "clock", "অ্যালার্ম": "clock",
    "এলার্ম": "clock", "ঘড়িতে": "clock",
    # Angel One
    "angelone": "angelone", "angel": "angelone", "angeloneapp": "angelone",
    "অ্যাঞ্জেলওয়ান": "angelone", "এঞ্জেলওয়ান": "angelone", "অ্যাঞ্জেল": "angelone",
}

# Prefixes Bengali speakers add to app names ("ইউটিউব অ্যাপটা খোলো").
_APP_NOISE = (
    "অ্যাপটা", "অ্যাপটি", "অ্যাপ", "খোলো", "খুলে", "খুল", "চালাও", "চালু",
    "app", "open", "launch", "start", "please", "করো", "দাও", "একটু",
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _normalise_app(name: Any) -> str:
    text = str(name or "").strip().lower()
    text = re.sub(r"[\s\u200c\u200d_\-]+", "", text)
    for noise in _APP_NOISE:
        if noise in text and text != noise:
            text = text.replace(noise, "")
    return text


def normalise_international(number: Any, country_code: str = "+880") -> str:
    """Best-effort E.164 for WhatsApp links.

    ``01712345678`` -> ``+8801712345678``, ``9876543210`` -> ``+919876543210``,
    anything already starting with ``+`` is preserved. Falls back to
    ``country_code`` for shapes it does not recognise.
    """
    raw = normalise_number(number)
    if not raw:
        return ""
    if raw.startswith("+"):
        return raw
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""

    if len(digits) >= 12 and digits.startswith("880"):
        return "+" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return country_code + digits[1:]
    if len(digits) == 10:
        # Indian mobiles start 6-9; Bangladeshi numbers without the 0 start with 1.
        return ("+91" if digits[0] in "6789" else "+880") + digits
    if len(digits) == 13 and digits.startswith("880"):
        return "+" + digits
    return country_code + digits


class AdbAutomation:
    """App launching, WhatsApp deep links and screen reading over ADB."""

    def __init__(
        self,
        control: Optional[SystemControl] = None,
        runner: Any = run_command,
        settings: Optional[Any] = None,
        session: Any = None,
        adb_prefix: Sequence[str] = DEFAULT_ADB,
        timeout: float = ADB_TIMEOUT,
        vision_timeout: float = VISION_TIMEOUT,
        country_code: str = "+880",
        screen_dir: Optional[Path] = None,
        keep_screenshot: bool = False,
        clock: Any = None,
    ) -> None:
        self.settings = settings or default_settings
        self.control = control or SystemControl(settings=self.settings)
        self._runner = runner
        self._session = session
        self.adb_prefix = tuple(adb_prefix)
        self.timeout = float(timeout)
        self.vision_timeout = float(vision_timeout)
        self.country_code = country_code
        self.screen_dir = Path(screen_dir) if screen_dir else SCREEN_DIR
        self.keep_screenshot = bool(keep_screenshot)
        self.last_error: Optional[str] = None
        self.last_command: Optional[str] = None

    # -- plumbing ---------------------------------------------------------- #

    def _run(self, cmd: Sequence[str], timeout: Optional[float] = None) -> CommandResult:
        self.last_command = " ".join(str(part) for part in cmd)
        try:
            return self._runner(list(cmd), timeout or self.timeout)
        except TypeError:  # a fake runner that only accepts the command
            return self._runner(list(cmd))

    def _adb(self, *args: str) -> List[str]:
        return [*self.adb_prefix, *args]

    def _fail(self, error: str, action: str, **extra: Any) -> Dict[str, Any]:
        self.last_error = error
        log.warning("%s failed: %s", action, error)
        return {"ok": False, "action": action, "error": error, **extra}

    @staticmethod
    def _monkey_ok(result: CommandResult) -> bool:
        """``monkey`` exits 0 even when it finds nothing — read its output."""
        if not result.ok:
            return False
        text = (result.stdout + "\n" + result.stderr).lower()
        return not any(
            marker in text
            for marker in ("no activities found", "monkey aborted", "error:", "does not exist")
        )

    # ------------------------------------------------------------------ #
    # open_app
    # ------------------------------------------------------------------ #

    @staticmethod
    def resolve_app(app_name: Any) -> Optional[str]:
        """Map a spoken app name to a key in ``APPS`` (or None)."""
        text = _normalise_app(app_name)
        if not text:
            return None
        if text in APP_ALIASES:
            return APP_ALIASES[text]
        if text in APPS:
            return text
        for key, info in APPS.items():
            if text == _normalise_app(info.get("name_bn", "")) or text == key:
                return key
        # Direct package name (com.whatsapp) or a partial match.
        for key, info in APPS.items():
            if text == _normalise_app(info.get("package", "")):
                return key
        for alias, key in APP_ALIASES.items():
            if len(text) >= 3 and (text in alias or alias in text):
                return key
        best: Tuple[float, Optional[str]] = (0.0, None)
        for alias, key in APP_ALIASES.items():
            score = SequenceMatcher(None, text, alias).ratio()
            if score > best[0]:
                best = (score, key)
        return best[1] if best[0] >= 0.78 else None

    def _launch_plan(self, key: str) -> List[List[str]]:
        info = APPS[key]
        package = info.get("package")
        plans: List[List[str]] = []
        if package:
            plans.append(self._adb("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"))
        if package and info.get("activity"):
            plans.append(self._adb("am", "start", "-n", f"{package}/{info['activity']}"))
        for alt in info.get("alternatives", []):
            plans.append(self._adb("monkey", "-p", alt, "-c", "android.intent.category.LAUNCHER", "1"))
        intent = info.get("intent")
        if intent:
            action, _data = intent
            plans.append(self._adb("am", "start", "-a", action))
        if package:
            plans.append(
                self._adb("am", "start", "-a", "android.intent.action.MAIN",
                          "-c", "android.intent.category.LAUNCHER", "-p", package)
            )
        return plans

    def open_app(self, app_name: str) -> Dict[str, Any]:
        """Launch a known app — never a bank, UPI, wallet or trading app."""
        wanted = str(app_name or "").strip()
        if is_financial_target(wanted):
            self.last_error = "banking_app_blocked"
            log.warning("Refused financial app launch: %r", wanted)
            return financial_app_block(wanted)

        key = self.resolve_app(app_name)
        if key is None:
            wanted = str(app_name or "").strip() or "?"
            return self._fail(
                f"অ্যাপটা চিনতে পারিনি: {wanted}",
                "open_app",
                app=wanted,
                available=[info["name_bn"] for info in APPS.values()],
                message_bn=f"বস, '{wanted}' নামে কোনো অ্যাপ আমার জানা নেই।",
            )

        info = APPS[key]
        if is_financial_target(info.get("package")) or is_financial_target(info.get("name_bn")):
            self.last_error = "banking_app_blocked"
            log.warning("Refused financial app launch via key %s (%s)", key, info.get("package"))
            return financial_app_block(wanted or info.get("name_bn") or key)

        attempts: List[Dict[str, Any]] = []
        for cmd in self._launch_plan(key):
            result = self._run(cmd, timeout=self.timeout)
            looks_ok = self._monkey_ok(result) if cmd[2] == "monkey" else result.ok
            attempts.append(
                {
                    "cmd": " ".join(cmd),
                    "ok": looks_ok,
                    "error": None if looks_ok else result.describe(),
                }
            )
            if looks_ok:
                self.last_error = None
                log.info("Opened %s via %s", key, attempts[-1]["cmd"])
                return {
                    "ok": True,
                    "action": "open_app",
                    "app": key,
                    "name_bn": info["name_bn"],
                    "package": info.get("package"),
                    "detail": attempts[-1]["cmd"],
                    "attempts": attempts,
                    "message_bn": f"বস, {info['name_bn']} খুলছি।",
                }

        self.last_error = attempts[-1]["error"] if attempts else "no launch method available"
        return self._fail(
            self.last_error or "could not launch the app",
            "open_app",
            app=key,
            name_bn=info["name_bn"],
            package=info.get("package"),
            attempts=attempts,
            message_bn=f"বস, {info['name_bn']} খোলা গেল না — ফোনে অ্যাডবি ঠিক আছে কি না দেখুন।",
        )

    # ------------------------------------------------------------------ #
    # WhatsApp
    # ------------------------------------------------------------------ #

    def _whatsapp_url(self, phone: str, message: str) -> str:
        # api.whatsapp.com wants the country code without the leading '+'.
        text = f"&text={quote(message, safe='')}" if message else ""
        return WHATSAPP_URL.format(phone=phone.lstrip("+"), text=text)

    def _open_url(self, url: str) -> Dict[str, Any]:
        """Try the ADB VIEW intent, then Termux's own URL opener."""
        attempts: List[Dict[str, Any]] = []
        adb = self._run(self._adb("am", "start", "-a", "android.intent.action.VIEW", "-d", url))
        attempts.append(
            {"via": "adb", "cmd": self.last_command, "ok": adb.ok, "error": None if adb.ok else adb.describe()}
        )
        if adb.ok:
            return {"ok": True, "via": "adb", "attempts": attempts}

        opener = self._run(["termux-open-url", url])
        attempts.append(
            {"via": "termux-open-url", "cmd": self.last_command, "ok": opener.ok,
             "error": None if opener.ok else opener.describe()}
        )
        if opener.ok:
            return {"ok": True, "via": "termux-open-url", "attempts": attempts}

        return {"ok": False, "via": None, "attempts": attempts, "error": opener.describe()}

    def _press_send(self, send_button: Optional[Tuple[int, int]]) -> Optional[Dict[str, Any]]:
        """Best-effort send: tap coordinates when known, else KEYCODE_ENTER."""
        if send_button:
            try:
                x, y = int(send_button[0]), int(send_button[1])
            except (TypeError, ValueError):
                return {"ok": False, "error": "invalid send_button coordinates"}
            result = self._run(self._adb("input", "tap", str(x), str(y)))
            return {
                "strategy": "tap",
                "cmd": self.last_command,
                "ok": result.ok,
                "error": None if result.ok else result.describe(),
            }
        result = self._run(self._adb("input", "keyevent", "66"))
        return {
            "strategy": "keyevent-enter",
            "cmd": self.last_command,
            "ok": result.ok,
            "error": None if result.ok else result.describe(),
        }

    def send_whatsapp(
        self,
        contact: Optional[str] = None,
        number: Optional[str] = None,
        message: str = "",
        press_send: bool = False,
        send_button: Optional[Tuple[int, int]] = None,
    ) -> Dict[str, Any]:
        """Open a WhatsApp chat with the text prefilled (and optionally send it)."""
        resolved = self.control.resolve_contact(contact=contact, number=number)
        if not resolved.get("ok"):
            query = (contact or number or "?").strip()
            return self._fail(
                resolved.get("error") or f"contact not found: {query}",
                "whatsapp",
                contact=query,
                message_bn=f"বস, '{query}' কে খুঁজে পেলাম না।",
            )

        phone = normalise_international(resolved.get("number"), self.country_code)
        if not phone:
            return self._fail(
                "no usable phone number for WhatsApp",
                "whatsapp",
                contact=contact or number,
                message_bn="বস, এই কন্ট্যাক্টের কোনো ফোন নম্বর নেই।",
            )

        body = str(message or "").strip()
        if contains_secret(body):
            self.last_error = "secret_message_blocked"
            log.warning("Refused WhatsApp message containing a secret")
            return {
                "ok": False,
                "action": "whatsapp",
                "error": "secret_message_blocked",
                "blocked": True,
                "name": (resolved.get("name") or phone).strip(),
                "reply_bn": SECRET_REFUSAL_BN,
                "message_bn": SECRET_REFUSAL_BN,
            }

        url = self._whatsapp_url(phone, body)
        opened = self._open_url(url)
        name = (resolved.get("name") or phone).strip()

        if not opened["ok"]:
            return self._fail(
                opened.get("error") or "could not open WhatsApp",
                "whatsapp",
                name=name,
                number=phone,
                message=body,
                url=url,
                attempts=opened.get("attempts", []),
                message_bn=f"বস, {name}-কে হোয়াটসঅ্যাপে মেসেজটা খোলা গেল না।",
            )

        result: Dict[str, Any] = {
            "ok": True,
            "action": "whatsapp",
            "name": name,
            "number": phone,
            "message": body,
            "url": url,
            "via": opened["via"],
            "attempts": opened.get("attempts", []),
            "sent": False,
            "message_bn": (
                f"বস, {name}-কে হোয়াটসঅ্যাপে মেসেজ লিখে রেখেছি।"
                if body else f"বস, {name}-এর হোয়াটসঅ্যাপ চ্যাট খুলছি।"
            ),
        }

        if press_send:
            send = self._press_send(send_button)
            result["send_attempt"] = send
            result["sent"] = bool(send.get("ok"))
            if send.get("ok"):
                result["message_bn"] = f"বস, {name}-কে হোয়াটসঅ্যাপে মেসেজ পাঠিয়ে দিয়েছি।"
            else:
                result["message_bn"] = (
                    f"বস, {name}-এর চ্যাট খুলেছি, কিন্তু মেসেজটা নিজে পাঠাতে হবে — "
                    "সেন্ড বাটনের জায়গাটা আমি জানি না।"
                )

        return result

    # ------------------------------------------------------------------ #
    # read_screen
    # ------------------------------------------------------------------ #

    def screenshot(self, remote: str = "/sdcard/orbit_screen.png") -> Dict[str, Any]:
        """Capture the screen and pull it locally. Returns the local path."""
        try:
            self.screen_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover
            return self._fail(f"cannot create {self.screen_dir}: {exc}", "read_screen")

        local = Path(tempfile.mkstemp(prefix="orbit_screen_", suffix=".png", dir=str(self.screen_dir))[1])
        captured = self._run(self._adb("screencap", "-p", remote), timeout=self.timeout)
        if not captured.ok:
            local.unlink(missing_ok=True)
            return self._fail(
                f"screencap failed: {captured.describe()}", "read_screen", attempts=[self.last_command]
            )

        pulled = self._run(["adb", "pull", remote, str(local)], timeout=self.timeout)
        if not pulled.ok:
            local.unlink(missing_ok=True)
            return self._fail(
                f"adb pull failed: {pulled.describe()}", "read_screen", attempts=[self.last_command]
            )

        try:
            size = local.stat().st_size
            head = local.open("rb").read(8)
        except OSError as exc:
            local.unlink(missing_ok=True)
            return self._fail(f"cannot read screenshot: {exc}", "read_screen")

        if size < MIN_PNG_BYTES or head != _PNG_MAGIC:
            local.unlink(missing_ok=True)
            return self._fail(
                f"screenshot is not a valid PNG ({size} bytes)", "read_screen", attempts=[self.last_command]
            )

        self._cleanup_remote(remote)
        return {"ok": True, "path": str(local), "bytes": size}

    def _cleanup_remote(self, remote: str) -> None:
        try:
            self._run(self._adb("rm", "-f", remote), timeout=10.0)
        except Exception:  # pragma: no cover - cleanup is optional
            log.debug("Could not remove %s from the phone", remote)

    def _describe(self, image_b64: str, prompt_bn: str) -> Dict[str, Any]:
        """Send the screenshot to Gemini 2.5 Flash vision."""
        if not self.settings.gemini_api_key:
            return self._fail("GEMINI_API_KEY is not set — screen vision unavailable", "read_screen")

        session = self._session
        if session is None:
            if requests is None:  # pragma: no cover
                return self._fail("requests is not installed", "read_screen")
            session = requests.Session()

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": f"{VISION_INSTRUCTION}\n\nUser request: {prompt_bn or VISION_PROMPT_BN}"},
                        {"inline_data": {"mime_type": "image/png", "data": image_b64}},
                    ]
                }
            ],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 400},
        }
        url = GEMINI_URL.format(model=GEMINI_MODEL)

        last_error = "vision request failed"
        for attempt in range(2):
            try:
                response = session.post(
                    url,
                    params={"key": self.settings.gemini_api_key},
                    json=payload,
                    timeout=self.vision_timeout,
                )
            except Exception as exc:
                last_error = f"vision request failed: {exc}"
                break

            status = getattr(response, "status_code", 0)
            if status in (429, 500, 502, 503, 504) and attempt == 0:
                last_error = f"vision HTTP {status}"
                continue
            if status != 200:
                last_error = f"vision HTTP {status}: {getattr(response, 'text', '')[:160]}"
                break

            try:
                data = response.json()
                parts = data["candidates"][0]["content"]["parts"]
                text = " ".join(str(part.get("text", "")) for part in parts).strip()
            except Exception as exc:
                last_error = f"unreadable vision response: {exc}"
                break

            if not text:
                last_error = "vision returned an empty description"
                break
            return {"ok": True, "text": text, "model": GEMINI_MODEL}

        return self._fail(last_error, "read_screen")

    def screen_guard(self) -> Dict[str, Any]:
        """Inspect the focused window before any screenshot is taken.

        Fail-closed: if ``dumpsys window`` cannot be read, the focused package
        cannot be parsed, ``FLAG_SECURE`` is set or the app is financial, the
        returned dict has ``blocked=True`` and a Bengali ``reply_bn``.
        """
        result = self._run(self._adb("shell", "dumpsys", "window"), timeout=min(self.timeout, 15.0))
        if not result.ok:
            info = evaluate_screen("", unavailable=True)
            info["error"] = result.describe()
            log.warning("Screen guard unavailable: %s", info["error"])
            return info
        info = evaluate_screen(result.output)
        if info.get("blocked"):
            log.warning(
                "Screen guard blocked capture (package=%s reason=%s)",
                info.get("package"), info.get("reason"),
            )
        return info

    def read_screen(self, prompt_bn: str = VISION_PROMPT_BN) -> Dict[str, Any]:
        """Screenshot the phone and describe it — unless a protected window is open."""
        guard = self.screen_guard()
        if guard.get("blocked"):
            self.last_error = "banking_screen_blocked"
            return {
                "ok": False,
                "action": "read_screen",
                "error": "banking_screen_blocked",
                "blocked": True,
                "reason": guard.get("reason"),
                "package": guard.get("package"),
                "secure": bool(guard.get("secure")),
                "description_bn": "",
                "reply_bn": SCREEN_REFUSAL_BN,
            }

        shot = self.screenshot()
        if not shot.get("ok"):
            return {
                **shot,
                "description_bn": "",
                "reply_bn": "বস, স্ক্রিনের ছবি নিতে পারলাম না।",
            }

        path = Path(shot["path"])
        try:
            image_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            described = self._describe(image_b64, prompt_bn)
        finally:
            if not self.keep_screenshot:
                path.unlink(missing_ok=True)

        if not described.get("ok"):
            return {
                "ok": False,
                "action": "read_screen",
                "error": described.get("error"),
                "image_path": None,
                "description_bn": "",
                "reply_bn": "বস, স্ক্রিনটা পড়তে পারলাম না — ভিশন সার্ভিসে সমস্যা হচ্ছে।",
            }

        description = ensure_boss_address(described["text"])
        log.info("Screen described in %d chars", len(description))
        return {
            "ok": True,
            "action": "read_screen",
            "description_bn": description,
            "reply_bn": ensure_boss_address(description),
            "model": described.get("model", GEMINI_MODEL),
            "prompt_bn": prompt_bn or VISION_PROMPT_BN,
            "image_bytes": shot.get("bytes"),
            "image_kept": bool(self.keep_screenshot),
        }


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    automation = AdbAutomation()
    command = sys.argv[1] if len(sys.argv) > 1 else "screen"

    if command == "app" and len(sys.argv) > 2:
        print(json.dumps(automation.open_app(" ".join(sys.argv[2:])), ensure_ascii=False, indent=2))
    elif command == "whatsapp" and len(sys.argv) > 3:
        print(json.dumps(
            automation.send_whatsapp(contact=sys.argv[2], message=" ".join(sys.argv[3:])),
            ensure_ascii=False, indent=2,
        ))
    else:
        print(json.dumps(automation.read_screen(), ensure_ascii=False, indent=2))
