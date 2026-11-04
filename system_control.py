"""
system_control.py — ORBIT's hands on the phone (Termux:API).

Every function here is a thin, defensive wrapper around one Termux:API
binary.  The rules are:

* validate the input **again** (brain.py already did, but device code must not
  trust its caller),
* run with a hard timeout and capture stderr,
* return a plain dict that is JSON-serialisable (so Telegram can forward it),
* never raise because a binary, permission or radio is missing.

    from system_control import SystemControl
    sc = SystemControl()
    sc.torch("on")                  # {'ok': True, 'state': 'on', ...}
    sc.battery_status()             # {'ok': True, 'percentage': 87, ...}
    sc.set_volume(40)               # 0-100 -> music stream max volume
    sc.toggle_wifi("off")
    sc.find_contact("রহিম")          # {'ok': True, 'name': 'Rahim', 'number': '017...'}

CLI (handy on the phone):
    python system_control.py battery
    python system_control.py torch on
    python system_control.py volume 40 music
    python system_control.py wifi off
    python system_control.py contact Rahim
    python system_control.py monitor 300
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from config import BASE_DIR, settings as default_settings

log = logging.getLogger("orbit.system")

CONTACT_TTL = 300.0          # seconds to trust the cached contact list
DEFAULT_TIMEOUT = 15.0
CONTACTS_TIMEOUT = 40.0      # large address books are slow on cheap phones
LOW_BATTERY = 20
FULL_BATTERY = 100
ALERT_COOLDOWN = 1800.0      # don't nag more than once per 30 min

BN_DIGITS = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")
ASCII_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

BATTERY_STATUS_BN = {
    "CHARGING": "চার্জ হচ্ছে",
    "DISCHARGING": "চার্জ কমছে",
    "FULL": "পুরো চার্জ হয়েছে",
    "NOT_CHARGING": "চার্জ হচ্ছে না",
    "UNKNOWN": "অজানা অবস্থা",
}
PLUGGED_BN = {
    "UNPLUGGED": "চার্জার খোলা",
    "PLUGGED_AC": "চার্জারে লাগানো",
    "PLUGGED_USB": "ইউএসবি-তে লাগানো",
    "PLUGGED_WIRELESS": "ওয়্যারলেস চার্জারে",
}

# Rough Bengali -> Latin transliteration, only good enough for fuzzy names:
# "রহিম" -> "rahim" so a Banglish query "rahim" still matches.
_TRANSLIT = {
    "অ": "o", "আ": "a", "ই": "i", "ঈ": "i", "উ": "u", "ঊ": "u", "ঋ": "ri",
    "এ": "e", "ঐ": "oi", "ও": "o", "ঔ": "ou",
    "ক": "k", "খ": "kh", "গ": "g", "ঘ": "gh", "ঙ": "ng",
    "চ": "ch", "ছ": "chh", "জ": "j", "ঝ": "jh", "ঞ": "n",
    "ট": "t", "ঠ": "th", "ড": "d", "ঢ": "dh", "ণ": "n",
    "ত": "t", "থ": "th", "দ": "d", "ধ": "dh", "ন": "n",
    "প": "p", "ফ": "f", "ব": "b", "ভ": "v", "ম": "m",
    "য": "j", "র": "r", "ল": "l", "শ": "sh", "ষ": "sh", "স": "s", "হ": "h",
    "ড়": "r", "ঢ়": "rh", "য়": "y", "ৎ": "t", "ং": "ng", "ঃ": "h", "ঁ": "",
    "া": "a", "ি": "i", "ী": "i", "ু": "u", "ূ": "u", "ৃ": "ri",
    "ে": "e", "ৈ": "oi", "ো": "o", "ৌ": "ou", "্": "",
    "০": "0", "১": "1", "২": "2", "৩": "3", "৪": "4",
    "৫": "5", "৬": "6", "৭": "7", "৮": "8", "৯": "9",
}


# --------------------------------------------------------------------------- #
# Generic helpers (shared with security_guard.py)
# --------------------------------------------------------------------------- #

@dataclass
class CommandResult:
    """Outcome of one Termux:API call."""

    code: int
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None  # set when the process never ran

    @property
    def ok(self) -> bool:
        return self.error is None and self.code == 0

    @property
    def output(self) -> str:
        return (self.stdout or "").strip()

    def describe(self) -> str:
        if self.error:
            return self.error
        tail = (self.stderr or self.stdout or "").strip().replace("\n", " ")
        return tail[:200] if tail else f"exit code {self.code}"


def which_binary(name: str) -> Optional[str]:
    """Absolute path of a Termux:API binary, or None when not installed."""
    return shutil.which(name)


def run_command(cmd: Sequence[str], timeout: float = DEFAULT_TIMEOUT) -> CommandResult:
    """Run a command with a timeout. Never raises."""
    if not cmd:
        return CommandResult(code=2, error="empty command")

    binary = which_binary(cmd[0]) if not cmd[0].startswith("/") else None
    if binary is None and not cmd[0].startswith("/"):
        hint = (
            "pkg install termux-api"
            if cmd[0].startswith("termux-")
            else f"{cmd[0]} not found"
        )
        return CommandResult(code=127, error=f"{cmd[0]} not found ({hint})")

    executable = binary or cmd[0]
    try:
        completed = subprocess.run(
            [executable, *cmd[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return CommandResult(
            code=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
    except subprocess.TimeoutExpired:
        return CommandResult(code=124, error=f"{cmd[0]} timed out after {timeout:.0f}s")
    except FileNotFoundError:
        return CommandResult(code=127, error=f"{cmd[0]} not found (pkg install termux-api)")
    except Exception as exc:  # permissions, SELinux, OOM, ...
        return CommandResult(code=1, error=f"{cmd[0]} failed: {exc}")


def _extract_json(text: str) -> Any:
    """Parse JSON from Termux output that may contain warnings before it."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty output")
    try:
        return _json_loads(raw)
    except Exception:
        pass

    # Try the bracket that starts earliest (a warning line may contain ']').
    candidates = []
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = raw.find(opener), raw.rfind(closer)
        if start != -1 and end > start:
            candidates.append((start, raw[start : end + 1]))
    for _start, chunk in sorted(candidates, key=lambda item: item[0]):
        try:
            return _json_loads(chunk)
        except Exception:
            continue
    raise ValueError(f"no JSON in output: {raw[:160]!r}")


def _json_loads(text: str) -> Any:
    import json

    return json.loads(text)


def to_bengali_digits(value: Any) -> str:
    """123 -> '১২৩' (for messages that will be spoken aloud)."""
    return str(value).translate(BN_DIGITS)


def to_ascii_digits(value: Any) -> str:
    """'০১৭...' -> '017...'."""
    return str(value).translate(ASCII_DIGITS)


def normalise_number(number: Any) -> str:
    """Keep digits and a leading +, drop spaces/dashes/Bengali digits."""
    text = to_ascii_digits(number or "").strip()
    plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if not digits:
        return ""
    return ("+" + digits) if plus else digits


def transliterate(text: str) -> str:
    """Best-effort Bengali -> Latin so Banglish queries match Bengali names."""
    return "".join(_TRANSLIT.get(ch, ch) for ch in (text or ""))


def _normalise_name(text: str) -> str:
    """Lowercase, transliterate, strip punctuation/spaces for comparison."""
    value = transliterate((text or "").strip().lower())
    value = re.sub(r"[^a-z0-9]", "", value)
    return value


def _name_skeleton(text: str) -> str:
    """Consonant skeleton, so vowel-less transliteration still matches.

    ``রহিম`` transliterates to ``rhim`` (Bengali's inherent vowel is lost) while
    a Banglish query gives ``rahim``; both reduce to ``rhm`` here.
    """
    return re.sub(r"[aeiou]", "", _normalise_name(text))


# --------------------------------------------------------------------------- #
# SystemControl
# --------------------------------------------------------------------------- #

class SystemControl:
    """Device hardware, volume, Wi-Fi, contacts and battery alerts."""

    def __init__(
        self,
        settings: Optional[Any] = None,
        timeout: float = DEFAULT_TIMEOUT,
        contact_ttl: float = CONTACT_TTL,
        voice: Optional[Any] = None,
        runner: Any = run_command,
    ) -> None:
        self.settings = settings or default_settings
        self.timeout = timeout
        self.contact_ttl = contact_ttl
        self._voice = voice            # injected VoiceEngine (lazy otherwise)
        self._runner = runner          # injectable for tests

        self._contacts: List[Dict[str, str]] = []
        self._contacts_at: float = 0.0
        self.last_error: Optional[str] = None

        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()
        self._last_alert: Dict[str, float] = {}

    # -- plumbing ---------------------------------------------------------- #

    def _run(self, cmd: Sequence[str], timeout: Optional[float] = None) -> CommandResult:
        try:
            return self._runner(list(cmd), timeout or self.timeout)
        except TypeError:  # a fake runner that only takes the command
            return self._runner(list(cmd))

    @staticmethod
    def _fail(message: str, **extra: Any) -> Dict[str, Any]:
        return {"ok": False, "error": message, **extra}

    # ------------------------------------------------------------------ #
    # Torch
    # ------------------------------------------------------------------ #

    def torch(self, state: str = "on") -> Dict[str, Any]:
        """Switch the LED torch on or off (`termux-torch on|off`)."""
        value = str(state if state is not None else "").strip().lower()
        if value in ("1", "true", "enable", "start", "জ্বালাও", "চালু"):
            value = "on"
        elif value in ("0", "false", "disable", "stop", "নেভাও", "বন্ধ"):
            value = "off"
        if value not in ("on", "off"):
            return self._fail(f"invalid torch state: {state!r} (use 'on' or 'off')", state=state)

        result = self._run(["termux-torch", value], timeout=10)
        if result.ok:
            log.info("Torch %s", value)
            return {"ok": True, "state": value, "detail": f"termux-torch {value}"}

        self.last_error = result.describe()
        log.warning("Torch %s failed: %s", value, self.last_error)
        return self._fail(self.last_error, state=value)

    # ------------------------------------------------------------------ #
    # Battery
    # ------------------------------------------------------------------ #

    def battery_status(self) -> Dict[str, Any]:
        """Parsed `termux-battery-status` snapshot."""
        result = self._run(["termux-battery-status"], timeout=12)
        if not result.ok:
            self.last_error = result.describe()
            log.warning("battery-status failed: %s", self.last_error)
            return self._fail(self.last_error)

        try:
            data = _extract_json(result.output)
        except ValueError as exc:
            self.last_error = f"unreadable battery JSON: {exc}"
            return self._fail(self.last_error)
        if not isinstance(data, dict):
            return self._fail("unexpected battery payload")

        try:
            percentage = int(float(data.get("percentage", -1)))
        except (TypeError, ValueError):
            percentage = -1
        if percentage < 0:
            return self._fail("battery percentage missing from termux-battery-status")

        status = str(data.get("status") or "UNKNOWN").upper()
        plugged = str(data.get("plugged") or "UNPLUGGED").upper()
        try:
            temperature = round(float(data.get("temperature")), 1)
        except (TypeError, ValueError):
            temperature = None

        message = (
            f"ব্যাটারি {to_bengali_digits(percentage)} শতাংশ, "
            f"{BATTERY_STATUS_BN.get(status, 'অজানা অবস্থা')}, "
            f"{PLUGGED_BN.get(plugged, '')}".strip().rstrip("।") + "।"
        )
        if temperature is not None:
            message += f" তাপমাত্রা {to_bengali_digits(temperature)} ডিগ্রি।"

        return {
            "ok": True,
            "percentage": percentage,
            "status": status,
            "plugged": plugged,
            "temperature": temperature,
            "health": data.get("health"),
            "current": data.get("current"),
            "message_bn": message,
            "low": percentage < LOW_BATTERY,
            "full": percentage >= FULL_BATTERY,
        }

    @staticmethod
    def battery_alert_message(
        percentage: int,
        status: str = "",
        low: int = LOW_BATTERY,
        full: int = FULL_BATTERY,
    ) -> Optional[Tuple[str, str]]:
        """Pure decision: (kind, Bengali sentence) or None when nothing to say."""
        status = (status or "").upper()
        if percentage >= full or status == "FULL":
            return (
                "full",
                f"ব্যাটারি পুরো চার্জ হয়েছে, {to_bengali_digits(percentage)} শতাংশ। "
                "চার্জার খুলে ফেলুন।",
            )
        if percentage < low:
            return (
                "low",
                f"সতর্কতা! ব্যাটারি মাত্র {to_bengali_digits(percentage)} শতাংশ। "
                "এখনই চার্জে লাগান।",
            )
        return None

    def check_battery(
        self,
        low: int = LOW_BATTERY,
        full: int = FULL_BATTERY,
        notify: bool = True,
        force: bool = False,
        cooldown: float = ALERT_COOLDOWN,
    ) -> Dict[str, Any]:
        """Read the battery and speak a Bengali alert when it is low or full."""
        status = self.battery_status()
        if not status.get("ok"):
            return status

        alert = self.battery_alert_message(int(status["percentage"]), status.get("status", ""), low, full)
        status["alert"] = alert[0] if alert else None
        status["spoken"] = False

        if alert is None:
            self._last_alert.clear()  # condition cleared -> allow the next alert
            return status
        if not notify:
            return status

        kind, message = alert
        now = time.monotonic()
        previous = self._last_alert.get(kind, 0.0)
        if not force and previous and (now - previous) < cooldown:
            log.debug("Battery %s alert suppressed (cooldown)", kind)
            return status

        self._last_alert[kind] = now
        status["spoken"] = self._speak(message)
        status["alert_message_bn"] = message
        return status

    def _speak(self, text: str) -> bool:
        """Speak through VoiceEngine, importing it lazily (and never raising)."""
        try:
            if self._voice is None:
                from voice_engine import VoiceEngine

                self._voice = VoiceEngine()
            ok = self._voice.speak(text)
            if not ok:
                log.warning("Battery alert could not be spoken: %s", getattr(self._voice, "last_error", None))
            return bool(ok)
        except Exception as exc:  # voice must never break the monitor
            log.warning("Battery alert TTS failed: %s", exc)
            return False

    @property
    def monitor_running(self) -> bool:
        return bool(self._monitor_thread and self._monitor_thread.is_alive())

    def start_battery_monitor(
        self,
        interval: float = 300.0,
        low: int = LOW_BATTERY,
        full: int = FULL_BATTERY,
        cooldown: float = ALERT_COOLDOWN,
    ) -> threading.Thread:
        """Background thread polling the battery and speaking alerts."""
        if self.monitor_running:
            log.debug("Battery monitor already running")
            return self._monitor_thread  # type: ignore[return-value]

        self._monitor_stop.clear()

        def _loop() -> None:
            log.info("Battery monitor started (every %.0fs)", interval)
            # First check immediately, then every interval until stopped.
            while True:
                try:
                    self.check_battery(low=low, full=full, cooldown=cooldown)
                except Exception as exc:  # pragma: no cover - defensive
                    log.warning("Battery monitor iteration failed: %s", exc)
                if self._monitor_stop.wait(max(5.0, interval)):
                    break
            log.info("Battery monitor stopped")

        self._monitor_thread = threading.Thread(
            target=_loop, name="orbit-battery", daemon=True
        )
        self._monitor_thread.start()
        return self._monitor_thread

    def stop_battery_monitor(self, timeout: float = 5.0) -> None:
        self._monitor_stop.set()
        thread = self._monitor_thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        self._monitor_thread = None

    # ------------------------------------------------------------------ #
    # Volume
    # ------------------------------------------------------------------ #

    def _stream_volumes(self) -> List[Dict[str, Any]]:
        """Current stream volumes as `[{'stream': 'music', 'volume': 8, 'max_volume': 15}, ...]`."""
        result = self._run(["termux-volume"], timeout=10)
        if not result.ok:
            raise RuntimeError(result.describe())

        data = _extract_json(result.output)
        entries: List[Dict[str, Any]] = []

        if isinstance(data, dict):
            # Some builds return {"music": {"volume": 8, "max_volume": 15}, ...}
            for name, item in data.items():
                if isinstance(item, dict):
                    entries.append({"stream": name, **item})
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("stream"):
                    entries.append(item)

        if not entries:
            raise RuntimeError("termux-volume returned no streams")
        return entries

    def set_volume(
        self,
        level: Optional[int] = None,
        stream: str = "music",
        direction: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Set a 0-100 volume on ``stream`` (or nudge it up/down/mute/unmute)."""
        stream = (stream or "music").strip().lower()
        try:
            streams = self._stream_volumes()
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("Volume read failed: %s", exc)
            return self._fail(self.last_error, stream=stream)

        entry = next((s for s in streams if str(s.get("stream", "")).lower() == stream), None)
        if entry is None:
            available = ", ".join(str(s.get("stream")) for s in streams)
            return self._fail(f"unknown audio stream {stream!r} (available: {available})", stream=stream)

        try:
            max_volume = int(entry.get("max_volume") or 15)
            current = int(entry.get("volume") or 0)
        except (TypeError, ValueError):
            return self._fail("termux-volume reported non-numeric volume", stream=stream)
        max_volume = max(1, max_volume)

        action = (direction or "").strip().lower()
        if level is None and action:
            if action == "mute":
                target_level = 0
            elif action == "unmute":
                target_level = 50
            elif action == "up":
                target_level = round((current / max_volume) * 100) + 10
            elif action == "down":
                target_level = round((current / max_volume) * 100) - 10
            else:
                return self._fail(f"invalid volume direction: {direction!r}", stream=stream)
        elif level is None:
            return self._fail("set_volume needs a level (0-100) or a direction", stream=stream)
        else:
            try:
                target_level = int(to_ascii_digits(level).strip())
            except (TypeError, ValueError):
                return self._fail(f"invalid volume level: {level!r}", stream=stream)

        target_level = max(0, min(100, target_level))
        target = max(0, min(max_volume, round(target_level / 100 * max_volume)))

        result = self._run(["termux-volume", stream, str(target)], timeout=10)
        if not result.ok:
            self.last_error = result.describe()
            log.warning("Volume set failed: %s", self.last_error)
            return self._fail(self.last_error, stream=stream)

        log.info("Volume %s -> %d/%d (%d%%)", stream, target, max_volume, target_level)
        return {
            "ok": True,
            "stream": stream,
            "level": target_level,
            "raw": target,
            "max_volume": max_volume,
            "direction": action or None,
            "message_bn": f"{stream} ভলিউম {to_bengali_digits(target_level)} শতাংশ করা হয়েছে।",
        }

    # ------------------------------------------------------------------ #
    # Wi-Fi
    # ------------------------------------------------------------------ #

    def toggle_wifi(self, state: str = "on") -> Dict[str, Any]:
        """Enable/disable Wi-Fi (`termux-wifi-enable`). Android 10+ may refuse."""
        value = str(state if state is not None else "").strip().lower()
        if value in ("1", "true", "enable", "start", "চালু"):
            value = "on"
        elif value in ("0", "false", "disable", "stop", "বন্ধ"):
            value = "off"
        if value not in ("on", "off"):
            return self._fail(f"invalid wifi state: {state!r} (use 'on' or 'off')", state=state)

        flag = "true" if value == "on" else "false"
        result = self._run(["termux-wifi-enable", flag], timeout=15)
        if result.ok:
            log.info("Wi-Fi %s", value)
            return {"ok": True, "state": value, "detail": f"termux-wifi-enable {flag}"}

        self.last_error = result.describe()
        log.warning("Wi-Fi %s failed: %s", value, self.last_error)
        return self._fail(self.last_error, state=value)

    # ------------------------------------------------------------------ #
    # Contacts
    # ------------------------------------------------------------------ #

    def list_contacts(self, force: bool = False) -> List[Dict[str, str]]:
        """Cached `termux-contact-list` (name/number pairs)."""
        fresh = (time.monotonic() - self._contacts_at) < self.contact_ttl
        if self._contacts and fresh and not force:
            return self._contacts

        result = self._run(["termux-contact-list"], timeout=CONTACTS_TIMEOUT)
        if not result.ok:
            self.last_error = result.describe()
            log.warning("Contact list failed: %s", self.last_error)
            return self._contacts if (self._contacts and fresh) else []

        try:
            data = _extract_json(result.output)
        except ValueError as exc:
            self.last_error = f"unreadable contact JSON: {exc}"
            log.warning(self.last_error)
            return self._contacts if (self._contacts and fresh) else []

        contacts: List[Dict[str, str]] = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                number = normalise_number(item.get("number"))
                if name or number:
                    contacts.append({"name": name, "number": number})

        if contacts:
            self._contacts = contacts
            self._contacts_at = time.monotonic()
            log.info("Cached %d contacts", len(contacts))
        else:
            self.last_error = "contact list is empty (permission denied?)"
        return self._contacts if (self._contacts and fresh) else contacts

    def find_contact(self, query: str) -> Dict[str, Any]:
        """Resolve a spoken name or number to one contact.

        Matching order: exact number -> exact name -> name substring ->
        fuzzy name/number score. Bengali names are transliterated so a
        Banglish query ("rahim") finds "রহিম".
        """
        raw = str(query or "").strip()
        if not raw:
            return self._fail("empty contact query", query=query)

        contacts = self.list_contacts()
        if not contacts:
            return self._fail(self.last_error or "no contacts available", query=raw)

        digits = normalise_number(raw) if re.fullmatch(r"[\d+\-\s()০-৯]+", raw) else ""
        if digits:
            match = self._match_number(contacts, digits)
            if match:
                return match
            return self._fail(f"no contact with number {digits}", query=raw)

        if not _normalise_name(raw):
            return self._fail(f"unusable contact query: {raw!r}", query=raw)

        scored: List[Tuple[float, Dict[str, str]]] = []
        for contact in contacts:
            score = self._score_name(raw, contact.get("name", ""))
            if score >= 0.62:
                scored.append((score, contact))

        if not scored:
            return self._fail(f"contact not found: {raw}", query=raw)

        scored.sort(key=lambda pair: (-pair[0], pair[1].get("name", "")))
        best_score, best = scored[0]
        others = [
            {"name": c.get("name"), "number": c.get("number"), "score": round(s, 3)}
            for s, c in scored[1:5]
        ]
        log.info("Contact %r -> %s (%s) score %.2f", raw, best.get("name"), best.get("number"), best_score)
        return {
            "ok": True,
            "query": raw,
            "name": best.get("name") or raw,
            "number": best.get("number") or "",
            "score": round(best_score, 3),
            "others": others,
        }

    @staticmethod
    def _score_name(query: str, name: str) -> float:
        """0-1 similarity between a spoken query and a contact name.

        Compares both the transliterated letters and the consonant skeleton,
        so "rahim" finds "Rahim Uddin" *and* "রহিম".
        """
        def compare(needle: str, key: str) -> float:
            if not needle or not key:
                return 0.0
            if needle == key:
                return 1.0
            if min(len(needle), len(key)) >= 3 and (needle in key or key in needle):
                return 0.9
            return SequenceMatcher(None, needle, key).ratio()

        # Letters are the stronger signal; the vowel-less skeleton is a
        # discounted fallback so it never outranks a real name match.
        letters = compare(_normalise_name(query), _normalise_name(name))
        skeleton = compare(_name_skeleton(query), _name_skeleton(name)) * 0.9
        return max(letters, skeleton)

    @staticmethod
    def _match_number(contacts: Iterable[Dict[str, str]], digits: str) -> Optional[Dict[str, Any]]:
        """Compare on the last 10 digits so +880/0 prefixes don't matter."""
        target = re.sub(r"\D", "", digits)[-10:]
        if not target:
            return None
        for contact in contacts:
            number = re.sub(r"\D", "", contact.get("number", ""))
            if number and number[-10:] == target:
                return {
                    "ok": True,
                    "query": digits,
                    "name": contact.get("name") or "",
                    "number": contact.get("number") or "",
                    "score": 1.0,
                    "others": [],
                }
        return None

    def resolve_contact(
        self,
        contact: Optional[str] = None,
        number: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Give call/sms/whatsapp one canonical `{name, number}`.

        A number that the user spoke out loud wins; otherwise the name is
        resolved against the address book.
        """
        direct = normalise_number(number)
        if direct and re.fullmatch(r"\+?\d{3,15}", direct):
            name = (contact or "").strip() or direct
            return {"ok": True, "name": name, "number": direct, "source": "spoken"}

        lookup = (contact or number or "").strip()
        if not lookup:
            return self._fail("no contact or number given")
        found = self.find_contact(lookup)
        if found.get("ok"):
            found["source"] = "contacts"
        return found


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _print(result: Any) -> None:
    import json

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    control = SystemControl()
    args = sys.argv[1:]

    if not args:
        print(__doc__)
        print("available:", {name: bool(which_binary(name)) for name in (
            "termux-torch", "termux-battery-status", "termux-volume",
            "termux-wifi-enable", "termux-contact-list",
        )})
    elif args[0] == "battery":
        _print(control.battery_status())
    elif args[0] == "torch":
        _print(control.torch(args[1] if len(args) > 1 else "on"))
    elif args[0] == "volume":
        _print(control.set_volume(
            int(args[1]) if len(args) > 1 and args[1].isdigit() else None,
            stream=args[2] if len(args) > 2 else "music",
            direction=args[1] if len(args) > 1 and not args[1].isdigit() else None,
        ))
    elif args[0] == "wifi":
        _print(control.toggle_wifi(args[1] if len(args) > 1 else "on"))
    elif args[0] == "contact":
        _print(control.find_contact(" ".join(args[1:])) if len(args) > 1 else control.list_contacts())
    elif args[0] == "monitor":
        interval = float(args[1]) if len(args) > 1 else 300.0
        print(f"battery monitor every {interval:.0f}s — Ctrl+C to stop")
        try:
            while True:
                _print(control.check_battery())
                time.sleep(interval)
        except KeyboardInterrupt:
            print("stopped")
    else:
        print(f"unknown command: {args[0]}")
