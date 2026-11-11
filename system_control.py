"""
system_control.py — ORBIT's hands on the PC.

No Termux, no Android binaries. Everything here is ordinary Python on Windows
(and still importable on Linux/macOS for development):

* battery + CPU/RAM/disk/battery health via :mod:`psutil`,
* system volume through Windows media keys (Linux: ``pactl``/``amixer``),
* Wi-Fi state through ``netsh`` (Linux: ``nmcli``),
* contact book from ``~/.orbit/contacts.json``,
* a background low-battery monitor that speaks Bengali alerts.

    from system_control import SystemControl
    sc = SystemControl()
    sc.battery_status()
    sc.system_health()
    sc.set_volume(40)
    sc.find_contact("রহিম")

Every method returns a JSON-safe dict, never raises, and reports a missing
device (desktop without a battery, no Wi-Fi adapter, …) as ``ok: False`` with a
Bengali ``message_bn`` instead of crashing the assistant.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from config import settings as default_settings
from pc_automation import PcAutomation

log = logging.getLogger("orbit.system")

CONTACT_TTL = 300.0
DEFAULT_TIMEOUT = 15.0
LOW_BATTERY = 20
FULL_BATTERY = 100
ALERT_COOLDOWN = 1800.0

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

# Windows virtual-key codes for the volume keys.
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF

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
    """Outcome of one shell command."""

    code: int
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None

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
    """Absolute path of an executable on PATH, or None."""
    return shutil.which(name)


def run_command(cmd: Sequence[str], timeout: float = DEFAULT_TIMEOUT) -> CommandResult:
    """Run a command with a timeout. Never raises."""
    if not cmd:
        return CommandResult(code=2, error="empty command")

    executable = str(cmd[0])
    if not os.path.isabs(executable) and which_binary(executable) is None:
        return CommandResult(code=127, error=f"{executable} not found on PATH")

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
        return CommandResult(code=124, error=f"{executable} timed out after {timeout:.0f}s")
    except FileNotFoundError:
        return CommandResult(code=127, error=f"{executable} not found on PATH")
    except Exception as exc:
        return CommandResult(code=1, error=f"{executable} failed: {exc}")


def _json_loads(text: str) -> Any:
    import json

    return json.loads(text)


def _extract_json(text: str) -> Any:
    """Parse JSON that may be preceded by warning lines."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty output")
    try:
        return _json_loads(raw)
    except Exception:
        pass

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


def to_bengali_digits(value: Any) -> str:
    return str(value).translate(BN_DIGITS)


def to_ascii_digits(value: Any) -> str:
    return str(value).translate(ASCII_DIGITS)


def normalise_number(number: Any) -> str:
    text = to_ascii_digits(number or "").strip()
    plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if not digits:
        return ""
    return ("+" + digits) if plus else digits


def transliterate(text: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in (text or ""))


def _normalise_name(text: str) -> str:
    value = transliterate((text or "").strip().lower())
    return re.sub(r"[^a-z0-9]", "", value)


def _name_skeleton(text: str) -> str:
    return re.sub(r"[aeiou]", "", _normalise_name(text))


# --------------------------------------------------------------------------- #
# SystemControl
# --------------------------------------------------------------------------- #

class SystemControl:
    """PC hardware, volume, Wi-Fi, contacts and battery alerts."""

    def __init__(
        self,
        settings: Optional[Any] = None,
        timeout: float = DEFAULT_TIMEOUT,
        contact_ttl: float = CONTACT_TTL,
        voice: Optional[Any] = None,
        runner: Any = run_command,
        psutil_module: Any = None,
        contacts_path: Optional[Path] = None,
        contacts: Optional[Iterable[Dict[str, str]]] = None,
        platform_name: Optional[str] = None,
        key_sender: Optional[Any] = None,
    ) -> None:
        self.settings = settings or default_settings
        self.timeout = timeout
        self.contact_ttl = contact_ttl
        self._voice = voice
        self._runner = runner
        self._psutil = psutil_module
        self.platform_name = platform_name or platform.system()
        self._key_sender = key_sender
        self.contacts_path = Path(
            contacts_path or (Path.home() / ".orbit" / "contacts.json")
        )

        self._contacts: List[Dict[str, str]] = []
        self._contacts_at: float = 0.0
        if contacts:
            for item in contacts:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                number = normalise_number(item.get("number"))
                if name or number:
                    self._contacts.append({"name": name, "number": number})
            if self._contacts:
                self._contacts_at = time.monotonic()
        self._volume_level = 50
        self.last_error: Optional[str] = None
        self._pc = PcAutomation(
            settings=self.settings,
            psutil_module=psutil_module,
            platform_name=self.platform_name,
        )

        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()
        self._last_alert: Dict[str, float] = {}

    # -- plumbing ---------------------------------------------------------- #

    def _run(self, cmd: Sequence[str], timeout: Optional[float] = None) -> CommandResult:
        try:
            return self._runner(list(cmd), timeout or self.timeout)
        except TypeError:
            return self._runner(list(cmd))

    def _load_psutil(self) -> Any:
        if self._psutil is None:
            try:
                import psutil  # type: ignore

                self._psutil = psutil
            except Exception as exc:
                self.last_error = f"psutil নেই — pip install psutil ({exc})"
                return None
        return self._psutil

    @property
    def is_windows(self) -> bool:
        return str(self.platform_name).lower().startswith("win")

    @staticmethod
    def _fail(message: str, **extra: Any) -> Dict[str, Any]:
        return {"ok": False, "error": message, **extra}

    # ------------------------------------------------------------------ #
    # Torch (not a PC capability)
    # ------------------------------------------------------------------ #

    def torch(self, state: str = "on") -> Dict[str, Any]:
        value = str(state if state is not None else "").strip().lower()
        if value in ("1", "true", "enable", "start", "জ্বালাও", "চালু"):
            value = "on"
        elif value in ("0", "false", "disable", "stop", "নেভাও", "বন্ধ"):
            value = "off"
        if value not in ("on", "off"):
            return self._fail(f"invalid torch state: {state!r} (use 'on' or 'off')", state=state)
        message = "বস, এই কম্পিউটারে টর্চ নেই — ফোনে গেলে টর্চ চালাতে পারব।"
        self.last_error = message
        return self._fail("torch_not_supported", state=value, message_bn=message)

    # ------------------------------------------------------------------ #
    # Battery
    # ------------------------------------------------------------------ #

    def battery_status(self) -> Dict[str, Any]:
        """Battery snapshot from psutil (desktops without one fail cleanly)."""
        module = self._load_psutil()
        if module is None:
            return self._fail(self.last_error or "psutil unavailable")

        try:
            battery = module.sensors_battery()
        except Exception as exc:
            self.last_error = f"battery read failed: {exc}"
            log.warning(self.last_error)
            return self._fail(self.last_error)

        if battery is None:
            self.last_error = "no battery detected on this PC"
            return self._fail(
                self.last_error,
                message_bn="বস, এই কম্পিউটারে ব্যাটারি নেই — সরাসরি পাওয়ারে চলছে।",
            )

        try:
            percentage = int(round(float(getattr(battery, "percent", -1))))
        except (TypeError, ValueError):
            percentage = -1
        if percentage < 0:
            return self._fail("battery percentage missing from psutil")

        plugged = bool(getattr(battery, "power_plugged", False))
        status = "FULL" if percentage >= FULL_BATTERY and plugged else (
            "CHARGING" if plugged else "DISCHARGING"
        )
        plug_key = "PLUGGED_AC" if plugged else "UNPLUGGED"

        message = (
            f"ব্যাটারি {to_bengali_digits(percentage)} শতাংশ, "
            f"{BATTERY_STATUS_BN.get(status, 'অজানা অবস্থা')}, "
            f"{PLUGGED_BN.get(plug_key, '')}".strip().rstrip("।") + "।"
        )
        return {
            "ok": True,
            "percentage": percentage,
            "status": status,
            "plugged": plug_key,
            "temperature": None,
            "health": None,
            "current": None,
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
        status = self.battery_status()
        if not status.get("ok"):
            return status

        alert = self.battery_alert_message(
            int(status["percentage"]), status.get("status", ""), low, full
        )
        status["alert"] = alert[0] if alert else None
        status["spoken"] = False

        if alert is None:
            self._last_alert.clear()
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
        try:
            if self._voice is None:
                from voice_engine import VoiceEngine

                self._voice = VoiceEngine()
            ok = self._voice.speak(text)
            if not ok:
                log.warning(
                    "Battery alert could not be spoken: %s",
                    getattr(self._voice, "last_error", None),
                )
            return bool(ok)
        except Exception as exc:
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
        if self.monitor_running:
            return self._monitor_thread  # type: ignore[return-value]

        self._monitor_stop.clear()

        def _loop() -> None:
            log.info("Battery monitor started (every %.0fs)", interval)
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
    # PC health
    # ------------------------------------------------------------------ #

    def system_stats(self) -> Dict[str, Any]:
        return self._pc.system_stats()

    def system_health(self) -> Dict[str, Any]:
        return self._pc.system_health()

    # ------------------------------------------------------------------ #
    # Volume
    # ------------------------------------------------------------------ #

    def _send_key(self, vk: int) -> bool:
        sender = self._key_sender
        if sender is not None:
            try:
                return bool(sender(vk))
            except Exception as exc:
                self.last_error = f"volume key failed: {exc}"
                return False

        if not self.is_windows:
            return False
        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, 2, 0)
            return True
        except Exception as exc:  # pragma: no cover - non-Windows ctypes
            self.last_error = f"volume key failed: {exc}"
            return False

    def _apply_volume(self, level: int, stream: str) -> Dict[str, Any]:
        """Set system volume. Windows: media keys; Linux: pactl/amixer."""
        if self.is_windows:
            delta = level - self._volume_level
            steps = min(50, abs(round(delta / 2.0)))
            key = VK_VOLUME_UP if delta > 0 else VK_VOLUME_DOWN
            sent = 0
            for _ in range(steps):
                if not self._send_key(key):
                    break
                sent += 1
            if steps == 0 and level == 0:
                self._send_key(VK_VOLUME_MUTE)
            if steps > 0 and sent == 0:
                return self._fail(self.last_error or "volume keys unavailable")
            self._volume_level = level
            return {"ok": True, "detail": f"media keys x{sent}", "steps": sent}

        for command in (
            ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"],
            ["amixer", "-D", "pulse", "sset", "Master", f"{level}%"],
        ):
            result = self._run(command, timeout=10)
            if result.ok:
                self._volume_level = level
                return {"ok": True, "detail": " ".join(command[:2])}
        self.last_error = result.describe() if "result" in locals() else "no mixer found"
        return self._fail(self.last_error)

    def set_volume(
        self,
        level: Optional[int] = None,
        stream: str = "music",
        direction: Optional[str] = None,
    ) -> Dict[str, Any]:
        stream = (stream or "music").strip().lower()
        if stream not in ("music", "master", "system"):
            return self._fail(
                f"unknown audio stream {stream!r} (PC has one master volume)", stream=stream
            )

        action = (direction or "").strip().lower()
        if level is None and action:
            if action == "mute":
                if self.is_windows:
                    self._send_key(VK_VOLUME_MUTE)
                target_level = 0
            elif action == "unmute":
                if self.is_windows:
                    self._send_key(VK_VOLUME_MUTE)
                target_level = max(30, self._volume_level or 30)
            elif action == "up":
                target_level = self._volume_level + 10
            elif action == "down":
                target_level = self._volume_level - 10
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
        applied = self._apply_volume(target_level, stream)
        if not applied.get("ok"):
            return self._fail(applied.get("error") or "volume failed", stream=stream)
        if action in ("up", "down", "unmute"):
            self._volume_level = target_level

        log.info("Volume -> %d%% (%s)", target_level, action or "absolute")
        return {
            "ok": True,
            "stream": stream,
            "level": target_level,
            "raw": target_level,
            "max_volume": 100,
            "direction": action or None,
            "message_bn": f"ভলিউম {to_bengali_digits(target_level)} শতাংশ করা হয়েছে।",
        }

    # ------------------------------------------------------------------ #
    # Wi-Fi
    # ------------------------------------------------------------------ #

    def wifi_status(self) -> Dict[str, Any]:
        if self.is_windows:
            result = self._run(["netsh", "wlan", "show", "interfaces"], timeout=10)
        else:
            result = self._run(["nmcli", "-t", "-f", "GENERAL.STATE", "general"], timeout=10)
        if not result.ok:
            return self._fail(result.describe())
        text = result.output
        connected = bool(re.search(r"(?im)^\s*state\s*:\s*connected", text)) or (
            "connected" in text.lower()
        )
        return {"ok": True, "connected": connected, "detail": text.splitlines()[:4]}

    def toggle_wifi(self, state: str = "on") -> Dict[str, Any]:
        value = str(state if state is not None else "").strip().lower()
        if value in ("1", "true", "enable", "start", "চালু"):
            value = "on"
        elif value in ("0", "false", "disable", "stop", "বন্ধ"):
            value = "off"
        if value not in ("on", "off"):
            return self._fail(f"invalid wifi state: {state!r} (use 'on' or 'off')", state=state)

        if self.is_windows:
            flag = "enabled" if value == "on" else "disabled"
            command = ["netsh", "interface", "set", "interface", "Wi-Fi", f"admin={flag}"]
        else:
            command = ["nmcli", "radio", "wifi", "on" if value == "on" else "off"]

        result = self._run(command, timeout=20)
        if result.ok:
            log.info("Wi-Fi %s", value)
            return {"ok": True, "state": value, "detail": " ".join(command)}
        self.last_error = result.describe()
        return self._fail(
            self.last_error,
            state=value,
            message_bn=(
                "বস, ওয়াই-ফাই বদলাতে অ্যাডমিন অনুমতি দরকার। "
                "Windows সেটিংস থেকে বদলাতে হবে।"
                if self.is_windows
                else None
            ),
        )

    # ------------------------------------------------------------------ #
    # Contacts (~/.orbit/contacts.json)
    # ------------------------------------------------------------------ #

    def list_contacts(self, force: bool = False) -> List[Dict[str, str]]:
        fresh = (time.monotonic() - self._contacts_at) < self.contact_ttl
        if self._contacts and fresh and not force:
            return self._contacts

        path = self.contacts_path
        if not path.is_file():
            self.last_error = (
                f"contacts file missing: {path} (create it with a JSON list of "
                '{"name": ..., "number": ...})'
            )
            log.warning(self.last_error)
            return self._contacts if (self._contacts and fresh) else []

        try:
            data = _extract_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.last_error = f"unreadable contacts JSON: {exc}"
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
            self.last_error = "contact list is empty"
        return self._contacts if (self._contacts and fresh) else contacts

    def find_contact(self, query: str) -> Dict[str, Any]:
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
        def compare(needle: str, key: str) -> float:
            if not needle or not key:
                return 0.0
            if needle == key:
                return 1.0
            if min(len(needle), len(key)) >= 3 and (needle in key or key in needle):
                return 0.9
            return SequenceMatcher(None, needle, key).ratio()

        letters = compare(_normalise_name(query), _normalise_name(name))
        skeleton = compare(_name_skeleton(query), _name_skeleton(name)) * 0.9
        return max(letters, skeleton)

    @staticmethod
    def _match_number(
        contacts: Iterable[Dict[str, str]], digits: str
    ) -> Optional[Dict[str, Any]]:
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
    elif args[0] == "battery":
        _print(control.battery_status())
    elif args[0] == "health":
        _print(control.system_health())
    elif args[0] == "volume":
        _print(control.set_volume(
            int(args[1]) if len(args) > 1 and args[1].isdigit() else None,
            direction=args[1] if len(args) > 1 and not args[1].isdigit() else None,
        ))
    elif args[0] == "wifi":
        _print(control.wifi_status() if len(args) < 2 else control.toggle_wifi(args[1]))
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
