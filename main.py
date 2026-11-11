"""
main.py — ORBIT's master orchestrator and CLI.

    python main.py                 # daemon + Bengali text CLI
    python main.py daemon --no-cli # headless 24/7 mode (Termux service)
    python main.py once "টর্চ জালাও"
    python main.py voice           # one mic recording -> action -> spoken reply
    python main.py briefing        # morning market + world news, spoken
    python main.py doctor          # readiness table for Boss

The daemon:
  * takes a Termux wake-lock (so Android does not freeze the process),
  * runs five supervised threads — battery alerts, call reception, notification
    responder, daily self-upgrade + morning briefing, Telegram long polling,
  * restarts a thread that crashes, and releases the wake-lock on
    SIGINT/SIGTERM/atexit.

Every device call is injected through the existing runner pattern, so the
whole supervisor and CLI can be tested on a desktop with no Termux binaries.
"""

from __future__ import annotations

import argparse
import atexit
import importlib.util
import json
import logging
import platform
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from actions import ActionDispatcher
from brain import Brain
from briefing_agent import BriefingAgent
from call_receptionist import CallReceptionist
from config import settings as default_settings
from memory_engine import MemoryEngine, get_memory
from notification_responder import NotificationResponder
from security_guard import SecurityGuard
from security_policy import security_report
from system_control import CommandResult, SystemControl, run_command, which_binary, to_bengali_digits
from telegram_controller import TelegramController
from voice_engine import VoiceEngine

log = logging.getLogger("orbit.main")

ORBIT_HOME = Path.home() / ".orbit"
LOG_PATH = ORBIT_HOME / "orbit.log"


def configure_logging(level: int = logging.INFO) -> Path:
    """Send logs to ``~/.orbit/orbit.log`` (cross-platform) and the console."""
    ORBIT_HOME.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)

    if not any(isinstance(handler, logging.FileHandler) for handler in root.handlers):
        file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    if not any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        for handler in root.handlers
    ):
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)
    return LOG_PATH


SERVICE_NAMES_BN: Dict[str, str] = {
    "battery": "ব্যাটারি",
    "reception": "রিসেপশনিস্ট",
    "notifications": "নোটিফিকেশন",
    "daily": "ডেইলি ব্রিফিং",
    "telegram": "Telegram",
}
DEFAULT_SERVICES: Sequence[str] = ("battery", "reception", "notifications", "daily", "telegram")

# Phone control (ADB) is optional on a PC; the assistant runs without it.

GOODBYE_WORDS = ("exit", "quit", "/quit", "bye", "বিদায়", "বন্ধ")


# --------------------------------------------------------------------------- #
# Wake lock
# --------------------------------------------------------------------------- #

class WakeLock:
    """Keep the PC awake while the daemon runs (Termux wake-lock successor).

    Windows uses ``SetThreadExecutionState``; other platforms are a harmless
    no-op so development and CI on Linux still work.
    """

    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001

    def __init__(
        self,
        settings: Optional[Any] = None,
        runner: Any = None,
        platform_name: Optional[str] = None,
    ) -> None:
        self.settings = settings or default_settings
        self.platform_name = platform_name or platform.system()
        self.held = False

    @property
    def is_windows(self) -> bool:
        return str(self.platform_name).lower().startswith("win")

    def _set_execution_state(self, flags: int) -> None:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if not kernel32.SetThreadExecutionState(flags):
            raise OSError("SetThreadExecutionState failed")

    def acquire(self) -> Dict[str, Any]:
        if not getattr(self.settings, "wake_lock", True):
            return {"ok": True, "skipped": "WAKE_LOCK=false"}
        if self.held:
            return {"ok": True, "skipped": "already held"}
        if not self.is_windows:
            self.held = True
            return {
                "ok": True,
                "skipped": f"not required on {self.platform_name}",
                "detail": "no-op",
            }
        try:
            self._set_execution_state(self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED)
            self.held = True
            log.info("Wake-lock acquired (SetThreadExecutionState)")
            return {"ok": True, "detail": "SetThreadExecutionState"}
        except Exception as exc:
            log.warning("Wake-lock failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    def release(self) -> Dict[str, Any]:
        if not self.held:
            return {"ok": True, "skipped": "not held"}
        try:
            if self.is_windows:
                self._set_execution_state(self.ES_CONTINUOUS)
            self.held = False
            log.info("Wake-lock released")
            return {"ok": True, "detail": "released"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------------- #
# Supervisor
# --------------------------------------------------------------------------- #

@dataclass
class Service:
    name: str
    target: Callable[[], Any]
    stop: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None
    restarts: int = 0
    runs: int = 0
    last_error: Optional[str] = None
    finished: bool = False

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())


class OrbitDaemon:
    """Owns the modules, the wake-lock and the five background services."""

    def __init__(
        self,
        settings: Optional[Any] = None,
        memory: Any = None,
        control: Any = None,
        brain: Any = None,
        voice: Any = None,
        dispatcher: Any = None,
        briefing: Any = None,
        security: Any = None,
        receptionist: Any = None,
        responder: Any = None,
        telegram: Any = None,
        wake_lock: Any = None,
        runner: Callable[..., CommandResult] = run_command,
        clock: Callable[[], float] = time.time,
        today: Optional[Callable[[], str]] = None,
        battery_interval: float = 300.0,
        notification_interval: float = 30.0,
        reception_interval: float = 2.0,
        daily_interval: float = 3600.0,
        restart_delay: float = 5.0,
    ) -> None:
        self.settings = settings or default_settings
        self.memory = memory if memory is not None else get_memory(self.settings.memory_db)
        self.control = control or SystemControl(settings=self.settings)
        self.brain = brain or Brain(settings=self.settings, memory=self.memory)
        self.voice = voice if voice is not None else VoiceEngine(settings=self.settings)
        self.dispatcher = dispatcher or ActionDispatcher(
            control=self.control,
            brain=self.brain,
            voice=self.voice,
            memory=self.memory,
            settings=self.settings,
        )
        self.briefing = briefing or BriefingAgent(settings=self.settings)
        self.security = security or SecurityGuard(settings=self.settings)
        self.receptionist = receptionist or CallReceptionist(
            brain=self.brain,
            voice=self.voice,
            memory=self.memory,
            control=self.control,
            runner=runner,
            settings=self.settings,
        )
        self.responder = responder or NotificationResponder(
            brain=self.brain,
            memory=self.memory,
            control=self.control,
            settings=self.settings,
            runner=runner,
        )
        self.telegram = telegram or TelegramController(
            dispatcher=self.dispatcher,
            settings=self.settings,
            memory=self.memory,
            voice=self.voice,
            briefing=self.briefing,
            security=self.security,
            control=self.control,
            receptionist=self.receptionist,
            status_provider=self.service_status,
        )
        self.wake_lock = wake_lock or WakeLock(settings=self.settings, runner=runner)

        self._runner = runner
        self.clock = clock
        self._today = today or (lambda: datetime.now().date().isoformat())
        self.battery_interval = max(5.0, float(battery_interval))
        self.notification_interval = max(5.0, float(notification_interval))
        self.reception_interval = max(0.5, float(reception_interval))
        self.daily_interval = max(60.0, float(daily_interval))
        self.restart_delay = max(0.0, float(restart_delay))

        self.last_daily: Optional[str] = None
        self._last_call_signature: Optional[str] = None
        self.services: Dict[str, Service] = {}
        self.started_at: Optional[float] = None

    # -- service registry -------------------------------------------------- #

    def register_service(self, name: str, target: Callable[[], Any]) -> Service:
        service = Service(name=name, target=target)
        self.services[name] = service
        return service

    def build_services(self, enabled: Optional[Sequence[str]] = None) -> Dict[str, Service]:
        wanted = list(enabled or DEFAULT_SERVICES)
        targets = {
            "battery": self._battery_loop,
            "reception": self._reception_loop,
            "notifications": self._notifications_loop,
            "daily": self._daily_loop,
            "telegram": self._telegram_loop,
        }
        for name in wanted:
            target = targets.get(name)
            if target is not None:
                self.register_service(name, target)
        return self.services

    def start_all(self, enabled: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """Start (or reuse) every requested service thread."""
        if not self.services:
            self.build_services(enabled)

        names = list(enabled) if enabled else list(self.services)
        started: List[str] = []
        skipped: Dict[str, str] = {}
        for name in names:
            service = self.services.get(name)
            if service is None:
                continue
            if service.running:
                continue
            if name in ("reception", "notifications") and platform.system().lower().startswith("win"):
                skipped[name] = "Android-only service — disabled on this PC"
                log.info("Service skipped: %s", skipped[name])
                continue
            if name == "telegram" and not getattr(self.settings, "telegram_ready", False):
                skipped[name] = "TELEGRAM_BOT_TOKEN / ADMIN_CHAT_ID missing"
                log.warning("Telegram service skipped: %s", skipped[name])
                continue
            service.stop.clear()
            service.finished = False
            service.thread = threading.Thread(
                target=self._supervise, args=(service,), name=f"orbit-{name}", daemon=True
            )
            service.thread.start()
            started.append(name)
            log.info("Service started: %s", name)

        self.started_at = self.clock()
        return {"ok": True, "started": started, "skipped": skipped, "services": list(self.services)}

    def start(self, enabled: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        wake = self.wake_lock.acquire()
        services = self.start_all(enabled)
        return {"ok": True, "wake_lock": wake, "services": services}

    def stop_all(self, timeout: float = 5.0) -> Dict[str, Any]:
        for service in self.services.values():
            service.stop.set()
        stopped: List[str] = []
        for name, service in self.services.items():
            if service.thread and service.thread.is_alive():
                service.thread.join(timeout=timeout)
            if not (service.thread and service.thread.is_alive()):
                stopped.append(name)
        return {"ok": True, "stopped": stopped}

    def shutdown(self, timeout: float = 5.0) -> Dict[str, Any]:
        """Stop threads, release the wake-lock. Safe to call more than once."""
        services = self.stop_all(timeout=timeout)
        wake = self.wake_lock.release()
        return {"ok": True, "services": services, "wake_lock": wake}

    # -- guard ------------------------------------------------------------- #

    def _supervise(self, service: Service) -> None:
        """Run a target; restart it (with backoff) if it crashes."""
        while not service.stop.is_set():
            try:
                service.runs += 1
                service.target()
                service.finished = True
                # A target that returns on its own is finished, not crashed.
                break
            except Exception as exc:  # pragma: no cover - timing-dependent
                service.restarts += 1
                service.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("Service %s crashed (restart #%d)", service.name, service.restarts)
                if service.stop.wait(min(60.0, self.restart_delay * max(1, service.restarts))):
                    break
        service.finished = True

    def _wait(self, name: str, seconds: float) -> bool:
        """Sleep, but wake immediately when the service is asked to stop."""
        service = self.services.get(name)
        if service is not None:
            return service.stop.wait(seconds)
        time.sleep(seconds)
        return False

    def _stop_requested(self, name: str) -> bool:
        service = self.services.get(name)
        return bool(service and service.stop.is_set())

    # -- service loops ----------------------------------------------------- #

    def _battery_loop(self) -> None:
        while not self._stop_requested("battery"):
            try:
                self.battery_tick()
            except Exception as exc:
                log.warning("Battery tick failed: %s", exc)
            self._wait("battery", self.battery_interval)

    def battery_tick(self) -> Dict[str, Any]:
        """One battery poll; push a Bengali alert to Boss when one fires."""
        result = self.control.check_battery(notify=True)
        alert = result.get("alert_message_bn")
        if alert:
            self._push("🔋 " + alert)
        return result

    def _reception_loop(self) -> None:
        while not self._stop_requested("reception"):
            try:
                self.reception_tick()
            except Exception as exc:
                log.warning("Reception tick failed: %s", exc)
            self._wait("reception", self.reception_interval)

    def reception_tick(self) -> Dict[str, Any]:
        """Poll the dialer only while auto-reception is on; push each answered call."""
        if not getattr(self.receptionist, "auto_reception", False):
            return {"ok": True, "skipped": "auto_reception off"}
        result = self.receptionist.monitor_once()
        if result.get("answered"):
            signature = f"{result.get('caller')}:{result.get('timestamp')}:{result.get('transcript_text')}"
            if signature != self._last_call_signature:
                self._last_call_signature = signature
                self.telegram.send_call_summary(result)
        return result

    def _notifications_loop(self) -> None:
        while not self._stop_requested("notifications"):
            try:
                self.notifications_tick()
            except Exception as exc:
                log.warning("Notification tick failed: %s", exc)
            self._wait("notifications", self.notification_interval)

    def notifications_tick(self) -> Dict[str, Any]:
        """Poll messaging notifications; push a compact digest for new ones."""
        result = self.responder.poll()
        messages = result.get("new_messages") or []
        if messages:
            lines = ["📩 নতুন মেসেজ, বস:"]
            for item in messages[:5]:
                app = item.get("app_bn") or item.get("app") or "অ্যাপ"
                sender = item.get("sender") or item.get("title") or "অজানা"
                text = (item.get("text") or "")[:160]
                lines.append(f"• {app} — {sender}: {text}")
            if result.get("replies"):
                lines.append(f"✅ অটো-রিপ্লাই: {to_bengali_digits(len(result['replies']))}টি")
            self._push("\n".join(lines))
        return result

    def _daily_loop(self) -> None:
        while not self._stop_requested("daily"):
            try:
                self.daily_tick()
            except Exception as exc:
                log.warning("Daily tick failed: %s", exc)
            self._wait("daily", self.daily_interval)

    def daily_tick(self, force: bool = False) -> Dict[str, Any]:
        """Run the self-upgrade + morning briefing at most once per day."""
        today = self._today()
        if not force and self.last_daily == today:
            return {"ok": True, "skipped": "already ran today", "date": today}

        upgrade = self.memory.daily_self_upgrade()
        brief = self.briefing.market_news()
        self.last_daily = today

        rule = (upgrade.get("rule") or {}).get("rule_text") or ""
        question = upgrade.get("question_bn") or ""
        lines = [f"🌅 সুপ্রভাত বস — {to_bengali_digits(today)}"]
        if rule:
            lines.append(f"📌 আজকের শেখা: {rule}")
        if question:
            lines.append(f"❓ চ্যালেঞ্জ: {question}")
        summary = brief.get("reply_bn") or brief.get("summary_bn")
        if summary:
            lines.append(summary)
        text = "\n\n".join(lines)

        self._push(text, important=True)
        try:
            self.voice.speak(summary or rule or "সুপ্রভাত বস।")
        except Exception as exc:
            log.warning("Morning briefing TTS failed: %s", exc)
        return {"ok": bool(upgrade.get("ok", True)), "date": today, "text": text, "upgrade": upgrade, "briefing": brief}

    def _telegram_loop(self) -> None:
        service = self.services.get("telegram")
        stop = service.stop if service else None
        self.telegram.poll_forever(stop_event=stop)

    # -- helpers ----------------------------------------------------------- #

    def _push(self, text: str, important: bool = False) -> Dict[str, Any]:
        if not getattr(self.telegram, "token", None) or self.telegram.admin_chat_id is None:
            log.info("Push skipped (Telegram not configured): %s", text.splitlines()[0][:80])
            return {"ok": False, "error": "telegram not configured"}
        try:
            return self.telegram.notify_boss(text)
        except Exception as exc:
            log.warning("Telegram push failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    def service_status(self) -> Dict[str, Any]:
        status: Dict[str, Any] = {}
        for name in DEFAULT_SERVICES:
            service = self.services.get(name)
            status[SERVICE_NAMES_BN.get(name, name)] = bool(service and service.running)
        return status

    def status(self) -> Dict[str, Any]:
        services = {
            name: {
                "running": service.running,
                "runs": service.runs,
                "restarts": service.restarts,
                "last_error": service.last_error,
            }
            for name, service in self.services.items()
        }
        return {
            "ok": True,
            "services": services,
            "wake_lock": bool(getattr(self.wake_lock, "held", False)),
            "telegram": self.telegram.diagnostics(),
            "memory": self.memory.stats() if self.memory is not None else {"ok": False},
            "uptime_seconds": None if self.started_at is None else int(self.clock() - self.started_at),
        }


# --------------------------------------------------------------------------- #
# CLI helpers
# --------------------------------------------------------------------------- #

def interactive_loop(
    dispatcher: Any,
    reader: Callable[[str], str] = input,
    writer: Callable[[str], None] = print,
    stop_event: Any = None,
) -> int:
    """Bengali text loop: type a command, get a spoken/printed reply."""
    writer("বস, Orbit তৈরি। কথা বলুন (বন্ধ করতে 'exit')।")
    handled = 0
    while True:
        if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
            break
        try:
            line = reader("বস: ")
        except (EOFError, KeyboardInterrupt):
            break
        text = str(line or "").strip()
        if not text:
            continue
        if text.lower() in GOODBYE_WORDS:
            writer("বস, বিদায়। আমি আছি।")
            break
        try:
            result = dispatcher.process(text)
            writer(str(result.get("reply_bn") or "বস, কিছু বলতে পারলাম না।"))
            if result.get("error"):
                writer(f"(কারণ: {result['error']})")
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("CLI dispatch failed")
            writer(f"বস, সমস্যা হয়েছে: {exc}")
        handled += 1
    return handled


def doctor_report(
    settings: Optional[Any] = None,
    memory: Any = None,
    runner: Callable[..., CommandResult] = run_command,
    which: Callable[[str], Optional[str]] = which_binary,
    pc: Any = None,
) -> Dict[str, Any]:
    """Readiness table: keys, DB, PC health, audio, Telegram, guardrail."""
    settings = settings or default_settings
    rows: List[Dict[str, str]] = []

    def row(label: str, value: str, status: str) -> None:
        rows.append({"label": label, "value": value, "status": status})

    env_file = Path(getattr(settings, "env_path", Path(".env")))
    row("`.env`", "পাওয়া গেছে" if env_file.is_file() else "নেই — `cp .env.example .env`",
        "ok" if env_file.is_file() else "warn")

    def key_status(name: str, present: bool) -> None:
        row(name, "সেট ✅" if present else "সেট নয়", "ok" if present else "warn")

    key_status("GEMINI_API_KEY", bool(getattr(settings, "gemini_api_key", "")))
    key_status("GROQ_API_KEY", bool(getattr(settings, "groq_api_key", "")))
    key_status("TELEGRAM_BOT_TOKEN", bool(getattr(settings, "telegram_bot_token", "")))
    admin = getattr(settings, "admin_chat_id", None)
    row("ADMIN_CHAT_ID", str(admin) if admin else "সেট নয়", "ok" if admin else "warn")

    engine = memory
    close_engine = False
    if engine is None:
        try:
            engine = MemoryEngine(db_path=getattr(settings, "memory_db", None))
            close_engine = True
        except Exception as exc:
            row("SQLite মেমোরি", f"খোলা যায়নি: {exc}", "fail")
            engine = None
    if engine is not None:
        try:
            stats = engine.stats()
            if stats.get("ok"):
                row("SQLite মেমোরি", f"{stats.get('episodes', 0)} এপিসোড, {stats.get('facts', 0)} ফ্যাক্ট", "ok")
            else:
                row("SQLite মেমোরি", "ডাটাবেস কাজ করছে না", "fail")
        except Exception as exc:
            row("SQLite মেমোরি", f"ত্রুটি: {exc}", "fail")
        finally:
            if close_engine:
                try:
                    engine.close()
                except Exception:
                    pass

    python_ok = sys.version_info >= (3, 9)
    row("Python", f"{platform.python_version()}", "ok" if python_ok else "fail")

    lock = security_report()
    row("Banking Security Lock", lock["label_bn"], "ok" if lock.get("enabled") else "fail")

    is_windows = platform.system().lower().startswith("win")
    row("Windows", f"{platform.system()} {platform.release()}", "ok" if is_windows else "warn")

    pc_tool = pc
    if pc_tool is None:
        try:
            from pc_automation import PcAutomation

            pc_tool = PcAutomation(settings=settings)
        except Exception:
            pc_tool = None
    health: Dict[str, Any] = {}
    if pc_tool is not None:
        try:
            health = pc_tool.system_health()
        except Exception as exc:
            health = {"ok": False, "error": str(exc)}
    if health.get("ok"):
        row(
            "PC Health",
            f"CPU {health.get('cpu_percent')}% · RAM {health.get('ram_percent')}% · "
            f"ডিস্ক {health.get('disk_free_gb')} জিবি খালি",
            "ok",
        )
    else:
        row("PC Health", str(health.get("error") or "psutil নেই (pip install psutil)")[:80], "warn")

    audio_ok = bool(importlib.util.find_spec("edge_tts")) and bool(
        importlib.util.find_spec("pygame")
    )
    row(
        "Voice (edge-tts+pygame)",
        "প্রস্তুত" if audio_ok else "pip install edge-tts pygame",
        "ok" if audio_ok else "warn",
    )
    mic_ok = bool(importlib.util.find_spec("sounddevice"))
    row(
        "Microphone",
        "প্রস্তুত" if mic_ok else "pip install sounddevice soundfile",
        "ok" if mic_ok else "warn",
    )

    adb_state, adb_value = "warn", "adb নেই (ফোন কন্ট্রোল ঐচ্ছিক)"
    try:
        result = runner(["adb", "devices"], 10)
        output = result.output or ""
        devices = [line for line in output.splitlines() if "\tdevice" in line]
        if result.ok and devices:
            adb_state, adb_value = "ok", f"{len(devices)}টি ফোন সংযুক্ত"
        elif result.ok:
            adb_state, adb_value = "warn", "ফোন সংযুক্ত নয় (ঐচ্ছিক)"
        else:
            adb_value = result.describe()[:80]
    except Exception as exc:
        adb_value = str(exc)[:80]
    row("ADB (optional phone)", adb_value, adb_state)

    has_ai = bool(getattr(settings, "gemini_api_key", "") or getattr(settings, "groq_api_key", ""))
    memory_row = next((r for r in rows if r["label"] == "SQLite মেমোরি"), None)
    ready = has_ai and bool(memory_row and memory_row["status"] == "ok") and python_ok
    return {
        "ok": ready,
        "rows": rows,
        "summary": "✅ বস, Orbit চালানোর জন্য প্রস্তুত।" if ready else "⚠️ বস, উপরের হলুদ/লাল জিনিসগুলো ঠিক করুন।",
    }


def print_doctor(report: Dict[str, Any], out: Callable[[str], None] = print) -> None:
    icons = {"ok": "✅", "warn": "⚠️", "fail": "❌"}
    out("বস, Orbit প্রস্তুতি রিপোর্ট")
    out("─" * 46)
    for item in report.get("rows") or []:
        out(f"{icons.get(item['status'], '•')} {item['label']:<24} {item['value']}")
    out("─" * 46)
    out(report.get("summary") or "")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orbit",
        description="Orbit — বাংলা ব্যক্তিগত AI সহকারী (Windows PC)",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("chat", help="ইন্টারঅ্যাকটিভ চ্যাট (ডিফল্ট)")

    daemon = sub.add_parser("daemon", help="সব সার্ভিস + CLI চালু করুন")
    daemon.add_argument("--no-cli", action="store_true", help="শুধু ব্যাকগ্রাউন্ড সার্ভিস (headless)")

    once = sub.add_parser("once", help="একটা কমান্ড চালান")
    once.add_argument("text", nargs="+", help="যেমন: once \"টর্চ জালাও\"")
    once.add_argument("--json", action="store_true", help="পুরো ফলাফল JSON-এ দেখান")

    voice = sub.add_parser("voice", help="একটা ভয়েস কমান্ড")
    voice.add_argument("--seconds", type=float, default=6.0, help="রেকর্ডিং সময় (default 6)")

    sub.add_parser("briefing", help="আজকের বাজার ও বিশ্ব খবর")

    sub.add_parser("health", help="সিপিইউ, র‍্যাম, ডিস্ক ও ব্যাটারি দেখুন")

    doctor = sub.add_parser("doctor", help="পরিবেশ পরীক্ষা করুন")
    doctor.add_argument("--strict", action="store_true", help="প্রস্তুত না হলে exit 1")

    return parser


def _with_default_command(argv: Optional[Sequence[str]]) -> List[str]:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        return ["chat"]
    known = (
        "chat", "daemon", "once", "voice", "briefing", "health", "doctor",
        "-h", "--help",
    )
    if args[0] not in known:
        return ["once", *args]
    return args


def main(
    argv: Optional[Sequence[str]] = None,
    daemon: Optional[OrbitDaemon] = None,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = print,
    stop_event: Any = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(_with_default_command(argv))
    command = args.command or "daemon"

    if command == "doctor":
        report = doctor_report()
        print_doctor(report, out)
        return 0 if (report["ok"] or not getattr(args, "strict", False)) else 1

    instance = daemon or OrbitDaemon()

    if command == "once":
        text = " ".join(args.text).strip()
        result = instance.dispatcher.process(text)
        out(str(result.get("reply_bn") or "বস, কিছু করতে পারলাম না।"))
        if getattr(args, "json", False):
            out(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("ok") else 1

    if command == "voice":
        try:
            transcript = instance.voice.listen(seconds=args.seconds)
        except Exception as exc:
            err(f"বস, মাইক্রোফোন কাজ করছে না: {exc}")
            return 1
        if not transcript:
            err("বস, কিছু শুনতে পাইনি। আবার বলুন।")
            return 1
        out(f"🎧 শুনলাম: {transcript}")
        result = instance.dispatcher.process(transcript)
        out(str(result.get("reply_bn") or "বস, কিছু করতে পারলাম না।"))
        try:
            instance.voice.speak(str(result.get("reply_bn") or ""))
        except Exception as exc:
            log.warning("TTS failed: %s", exc)
        return 0 if result.get("ok") else 1

    if command == "health":
        health = instance.control.system_health()
        out(str(health.get("summary_bn") or health.get("error") or "বস, স্বাস্থ্য তথ্য পাওয়া গেল না।"))
        return 0 if health.get("ok") else 1

    if command == "chat":
        interactive_loop(instance.dispatcher, writer=out, stop_event=stop_event)
        return 0

    if command == "briefing":
        result = instance.briefing.market_news()
        reply = str(result.get("reply_bn") or result.get("summary_bn") or "বস, আজ কোনো খবর পাওয়া যায়নি।")
        out(reply)
        try:
            instance.voice.speak(reply)
        except Exception as exc:
            log.warning("Briefing TTS failed: %s", exc)
        return 0 if result.get("ok") else 1

    # daemon
    stop = stop_event or threading.Event()

    def _stop(signum: int, frame: Any) -> None:
        log.info("Signal %s received — shutting down", signum)
        stop.set()

    previous: Dict[int, Any] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, _stop)
        except (ValueError, OSError):  # not in the main thread (tests)
            pass

    atexit.register(instance.shutdown)
    try:
        instance.start()
        if getattr(args, "no_cli", False) or not sys.stdin.isatty():
            out("বস, Orbit ব্যাকগ্রাউন্ডে চলছে। বন্ধ করতে Ctrl+C।")
            while not stop.wait(1.0):
                pass
        else:
            interactive_loop(instance.dispatcher, writer=out, stop_event=stop)
    except KeyboardInterrupt:
        out("বস, বিদায়।")
    finally:
        try:
            atexit.unregister(instance.shutdown)
        except Exception:
            pass
        instance.shutdown()
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    configure_logging()
    raise SystemExit(main())
