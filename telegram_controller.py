"""
telegram_controller.py — Boss-only Telegram bridge for ORBIT.

Zero extra dependencies: the Bot API is plain HTTPS, so `requests` is enough
(no `python-telegram-bot`). Long polling keeps the phone's battery happy and
needs no public URL or webhook.

Security model
--------------
Only ``settings.admin_chat_id`` may command ORBIT. Any other chat gets one
polite refusal and a ``SECURITY`` warning in the log; its text never reaches
the brain or the dispatcher. A missing ``ADMIN_CHAT_ID`` refuses *everyone* —
the safe failure mode for a device that can dial, SMS and open apps.

    from telegram_controller import TelegramController
    controller = TelegramController()
    controller.poll_forever()                 # daemon thread in main.py
    controller.notify_boss("বস, কাজ শেষ।")    # from any background thread

Handled: text, voice notes / audio, and the Boss commands
``/start`` ``/help`` ``/status`` ``/briefing`` ``/thinker`` ``/intruder``
``/receptionist on|off``. Intruder evidence is uploaded as a photo with the
Maps link in the caption; call summaries and proactive alerts can be pushed at
any time. Failures return ``{ok: False, error: ...}`` — never raise.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from actions import ActionDispatcher
from briefing_agent import BriefingAgent
from call_receptionist import CallReceptionist
from config import settings as default_settings
from security_guard import SecurityGuard
from system_control import SystemControl, to_bengali_digits

try:  # optional at import time so tests/desktop need no requests
    import requests  # type: ignore
except Exception:  # pragma: no cover - requests is in requirements.txt
    requests = None  # type: ignore

log = logging.getLogger("orbit.telegram")

API_ROOT = "https://api.telegram.org/bot{token}/{method}"
FILE_ROOT = "https://api.telegram.org/file/bot{token}/{path}"
MAX_MESSAGE = 4096
SAFE_CHUNK = 4000

WELCOME_BN = """বস, আমি Orbit — আপনার ব্যক্তিগত সহকারী। 🤖

আমি যা করতে পারি:
• 🎙️ ভয়েস নোট বা বাংলা লেখা পাঠান — আমি বুঝে কাজ করব
• 📞 কল দেওয়া, SMS/হোয়াটসঅ্যাপ পাঠানো
• 🔦 টর্চ, ভলিউম, ওয়াই-ফাই, ব্যাটারি
• 📱 অ্যাপ খোলা, স্ক্রিন পড়া
• 📊 বাজার ও বিশ্ব খবর (`/briefing`)
• 🧠 দিনের চিন্তা ও শেখা (`/thinker`)
• 🚨 ঘরের ছবি + লোকেশন (`/intruder`)
• 🔋 সিস্টেম রিপোর্ট (`/status`)
• ☎️ রিসেপশনিস্ট চালু/বন্ধ (`/receptionist on|off`)

শুধু আপনি এই বট চালাতে পারেন। কী করতে হবে বলুন, বস।"""

REFUSAL_BN = "দুঃখিত, আমি শুধু আমার বসের নির্দেশ মানি। এই চ্যাট ব্যবহার করতে পারবেন না।"
UNKNOWN_COMMAND_BN = "বস, এই কমান্ডটা আমি চিনি না। `/help` পাঠালে সব কমান্ড দেখতে পাবেন।"
NO_TOKEN_BN = "বস, Telegram বট টোকেন সেট করা নেই — `.env`-এ `TELEGRAM_BOT_TOKEN` দিন।"
NO_ADMIN_BN = "বস, `ADMIN_CHAT_ID` সেট করা নেই, তাই নিরাপত্তার জন্য কাউকেই বিশ্বাস করছি না।"
TRANSCRIBE_FAIL_BN = "বস, ভয়েস নোটটা বুঝতে পারলাম না। একটু পরে আবার পাঠান, অথবা লিখে পাঠান।"
DOWNLOAD_FAIL_BN = "বস, ফাইলটা নামাতে পারলাম না। আবার পাঠাবেন?"
EMPTY_TEXT_BN = "বস, কিছু তো বলুন — লেখা বা ভয়েস নোট পাঠান।"

COMMANDS: Dict[str, str] = {
    "/start": "welcome",
    "/help": "welcome",
    "/status": "status",
    "/briefing": "briefing",
    "/thinker": "thinker",
    "/intruder": "intruder",
    "/receptionist": "receptionist",
}


class TelegramController:
    """Admin-gated Bot API client + long-polling command loop."""

    def __init__(
        self,
        dispatcher: Any = None,
        settings: Optional[Any] = None,
        session: Any = None,
        memory: Any = None,
        voice: Any = None,
        briefing: Any = None,
        security: Any = None,
        control: Any = None,
        receptionist: Any = None,
        audio_dir: Optional[Path] = None,
        poll_timeout: int = 25,
        request_timeout: float = 40.0,
        reply_voice: bool = False,
        status_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        backoff: float = 3.0,
        max_message: int = SAFE_CHUNK,
    ) -> None:
        self.settings = settings or default_settings
        self._session = session
        self.dispatcher = dispatcher
        self.memory = memory
        self.voice = voice
        self.briefing = briefing
        self.security = security
        self.control = control
        self.receptionist = receptionist
        self.audio_dir = Path(audio_dir) if audio_dir else Path.home() / ".orbit" / "telegram"
        self.poll_timeout = max(0, int(poll_timeout))
        self.request_timeout = float(request_timeout)
        self.reply_voice = bool(reply_voice)
        self.status_provider = status_provider
        self.sleep = sleep
        self.clock = clock
        self.backoff = max(0.5, float(backoff))
        self.max_message = max(200, min(int(max_message), MAX_MESSAGE))

        self._offset: Optional[int] = None
        self._refused: set = set()
        self.last_error: Optional[str] = None
        self.last_update: Optional[Dict[str, Any]] = None
        self.started_at = self.clock()
        self.updates_handled = 0

    # ------------------------------------------------------------------ #
    # HTTP plumbing
    # ------------------------------------------------------------------ #

    @property
    def session(self) -> Any:
        if self._session is None:
            self._session = requests.Session() if requests is not None else None
        return self._session

    @property
    def token(self) -> str:
        return (self.settings.telegram_bot_token or "").strip()

    @property
    def admin_chat_id(self) -> Optional[int]:
        try:
            return int(self.settings.admin_chat_id) if self.settings.admin_chat_id is not None else None
        except (TypeError, ValueError):
            return None

    def is_authorized(self, chat_id: Any) -> bool:
        admin = self.admin_chat_id
        if admin is None:
            return False
        try:
            return int(chat_id) == admin
        except (TypeError, ValueError):
            return False

    def api_call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST one Bot API method. Never raises; never logs the token."""
        if not self.token:
            self.last_error = "TELEGRAM_BOT_TOKEN missing"
            return {"ok": False, "error": self.last_error}
        if self.session is None:
            self.last_error = "requests is not installed"
            return {"ok": False, "error": self.last_error}

        url = API_ROOT.format(token=self.token, method=method)
        try:
            if files is not None:
                response = self.session.post(
                    url, data=data or {}, files=files, timeout=self.request_timeout
                )
            else:
                response = self.session.post(
                    url, json=params or {}, timeout=self.request_timeout
                )
        except Exception as exc:
            self.last_error = f"{method}: {exc}"
            log.warning("Telegram %s failed: %s", method, exc)
            return {"ok": False, "error": self.last_error}

        try:
            payload = response.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        status = getattr(response, "status_code", 0)
        ok = bool(payload.get("ok")) and 200 <= int(status or 0) < 300
        if not ok:
            description = payload.get("description") or getattr(response, "text", "") or ""
            self.last_error = f"{method} HTTP {status}: {str(description)[:300]}"
            log.warning("Telegram %s rejected: %s", method, self.last_error)
            return {"ok": False, "error": self.last_error, "status": status}
        self.last_error = None
        return {"ok": True, "result": payload.get("result")}

    # ------------------------------------------------------------------ #
    # Sending
    # ------------------------------------------------------------------ #

    @staticmethod
    def split_message(text: str, limit: int = SAFE_CHUNK) -> List[str]:
        """Split long Bengali text on newlines, then hard-split, under the limit."""
        text = str(text or "").strip()
        if not text:
            return []
        limit = max(200, min(int(limit), MAX_MESSAGE))
        if len(text) <= limit:
            return [text]

        chunks: List[str] = []
        current = ""
        for line in text.splitlines(keepends=True):
            while len(line) > limit:  # one pathological line
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(line[:limit])
                line = line[limit:]
            if len(current) + len(line) > limit:
                chunks.append(current)
                current = ""
            current += line
        if current.strip():
            chunks.append(current)
        return [chunk.strip() for chunk in chunks if chunk.strip()]

    def send_message(
        self,
        chat_id: Any,
        text: str,
        disable_preview: bool = True,
    ) -> Dict[str, Any]:
        chunks = self.split_message(text, self.max_message)
        if not chunks:
            return {"ok": False, "error": "empty message"}
        results = []
        for chunk in chunks:
            results.append(
                self.api_call(
                    "sendMessage",
                    {
                        "chat_id": chat_id,
                        "text": chunk,
                        "disable_web_page_preview": bool(disable_preview),
                    },
                )
            )
            if not results[-1].get("ok"):
                break
        ok = all(item.get("ok") for item in results)
        return {"ok": ok, "parts": len(results), "error": None if ok else self.last_error}

    def send_photo(self, chat_id: Any, photo: Path, caption: str = "") -> Dict[str, Any]:
        path = Path(photo)
        if not path.is_file():
            return {"ok": False, "error": f"photo not found: {path}"}
        try:
            with path.open("rb") as handle:
                files = {"photo": (path.name, handle, "image/jpeg")}
                data = {"chat_id": str(chat_id)}
                if caption:
                    data["caption"] = str(caption)[:1024]
                return self.api_call("sendPhoto", files=files, data=data)
        except Exception as exc:
            log.warning("sendPhoto failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    def send_voice(self, chat_id: Any, audio: Path, caption: str = "") -> Dict[str, Any]:
        path = Path(audio)
        if not path.is_file():
            return {"ok": False, "error": f"audio not found: {path}"}
        try:
            with path.open("rb") as handle:
                files = {"voice": (path.name, handle, "audio/mpeg")}
                data = {"chat_id": str(chat_id)}
                if caption:
                    data["caption"] = str(caption)[:1024]
                return self.api_call("sendVoice", files=files, data=data)
        except Exception as exc:
            log.warning("sendVoice failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    def notify_boss(self, text: str, photo: Optional[Path] = None) -> Dict[str, Any]:
        """Proactive push for any background thread."""
        admin = self.admin_chat_id
        if admin is None:
            return {"ok": False, "error": "ADMIN_CHAT_ID missing"}
        if photo is not None:
            return self.send_photo(admin, photo, caption=text)
        return self.send_message(admin, text)

    # ------------------------------------------------------------------ #
    # Proactive helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _intruder_caption(result: Dict[str, Any]) -> str:
        parts = [str(result.get("summary_bn") or "বস, ঘরের চেক সম্পন্ন হয়েছে।")]
        maps = result.get("maps_url")
        if maps:
            parts.append(f"📍 লোকেশন: {maps}")
        return "\n\n".join(parts)

    def send_intruder_alert(self, result: Dict[str, Any]) -> Dict[str, Any]:
        admin = self.admin_chat_id
        if admin is None:
            return {"ok": False, "error": "ADMIN_CHAT_ID missing"}
        caption = self._intruder_caption(result)
        photo = result.get("photo_path")
        if photo and Path(str(photo)).is_file():
            sent = self.send_photo(admin, Path(str(photo)), caption=caption)
            if sent.get("ok"):
                return {**sent, "photo": True, "maps_url": result.get("maps_url")}
        sent = self.send_message(admin, caption)
        return {**sent, "photo": False, "maps_url": result.get("maps_url")}

    @staticmethod
    def _call_summary_text(result: Dict[str, Any]) -> str:
        caller = result.get("caller") or "অজানা নম্বর"
        summary = result.get("summary_bn") or "কলটি ধরতে পারিনি।"
        lines = [f"📞 কল সারাংশ — {caller}", summary]
        heard = result.get("transcript_text") or result.get("transcript")
        if heard and heard not in summary:
            lines.append(f"🎧 শোনা গেছে: {heard}")
        if result.get("ok") is False and result.get("error"):
            lines.append(f"⚠️ সমস্যা: {result['error']}")
        return "\n".join(lines)

    def send_call_summary(self, result: Dict[str, Any]) -> Dict[str, Any]:
        admin = self.admin_chat_id
        if admin is None:
            return {"ok": False, "error": "ADMIN_CHAT_ID missing"}
        return self.send_message(admin, self._call_summary_text(result))

    # ------------------------------------------------------------------ #
    # Boss commands
    # ------------------------------------------------------------------ #

    def _status_text(self) -> str:
        lines = ["বস, সার্ভিস রিপোর্ট 📊"]

        status = self._battery_line()
        lines.append(status)

        lines.append(self._memory_line())
        lines.append(self._services_line())

        uptime = max(0, int(self.clock() - self.started_at))
        minutes, seconds = divmod(uptime, 60)
        lines.append(
            f"⏱️ চলছে: {to_bengali_digits(minutes)} মিনিট {to_bengali_digits(seconds)} সেকেন্ড"
        )
        return "\n".join(lines)

    def _battery_line(self) -> str:
        if self.control is None:
            return "🔋 ব্যাটারি: নিয়ন্ত্রণ মডিউল নেই"
        try:
            battery = self.control.battery_status()
        except Exception as exc:
            log.warning("Battery status failed: %s", exc)
            return "🔋 ব্যাটারি: পাওয়া যায়নি"
        if not battery.get("ok"):
            return "🔋 ব্যাটারি: পাওয়া যায়নি"
        percentage = to_bengali_digits(battery.get("percentage", "?"))
        message = battery.get("message_bn") or ""
        return f"🔋 ব্যাটারি: {percentage}% — {message}" if message else f"🔋 ব্যাটারি: {percentage}%"

    def _memory_line(self) -> str:
        if self.memory is None:
            return "🧠 মেমোরি: সংযুক্ত নয়"
        try:
            stats = self.memory.stats()
        except Exception as exc:
            log.warning("Memory stats failed: %s", exc)
            return "🧠 মেমোরি: পাওয়া যায়নি"
        if not stats.get("ok", True):
            return "🧠 মেমোরি: ডাটাবেস পাওয়া যায়নি"
        return (
            "🧠 মেমোরি: "
            f"{to_bengali_digits(stats.get('episodes', 0))} এপিসোড, "
            f"{to_bengali_digits(stats.get('facts', 0))} ফ্যাক্ট, "
            f"{to_bengali_digits(stats.get('rules', 0))} নিয়ম"
        )

    def _services_line(self) -> str:
        if self.status_provider is not None:
            try:
                provided = self.status_provider() or {}
            except Exception as exc:
                log.warning("status_provider failed: %s", exc)
                provided = {}
            if provided:
                rendered = ", ".join(
                    f"{name} {'✅' if value else '⏸️'}" for name, value in provided.items()
                )
                return f"📡 সার্ভিস: {rendered}"

        reception = "✅" if getattr(self.receptionist, "auto_reception", False) else "⏸️"
        telegram = "✅" if (self.token and self.admin_chat_id is not None) else "⏸️"
        return f"📡 সার্ভিস: Telegram {telegram}, রিসেপশনিস্ট {reception}"

    def _briefing_text(self) -> str:
        briefing = self.briefing or BriefingAgent(settings=self.settings)
        self.briefing = briefing
        try:
            result = briefing.market_news()
        except Exception as exc:
            log.warning("Briefing failed: %s", exc)
            return "বস, এখন ব্রিফিং আনতে পারলাম না। একটু পরে আবার চেষ্টা করুন।"
        return str(result.get("reply_bn") or result.get("summary_bn") or "বস, আজ কোনো খবর পাওয়া যায়নি।")

    def _thinker_text(self) -> str:
        if self.memory is None:
            return "বস, মেমোরি সংযুক্ত না থাকায় আজকের চ্যালেঞ্জ তৈরি করতে পারলাম না।"
        try:
            result = self.memory.daily_self_upgrade()
        except Exception as exc:
            log.warning("daily_self_upgrade failed: %s", exc)
            return "বস, আজকের চ্যালেঞ্জ তৈরি করতে সমস্যা হয়েছে।"
        if not result.get("ok", True):
            return "বস, আজকের চ্যালেঞ্জ তৈরি করতে সমস্যা হয়েছে।"

        rule = (result.get("rule") or {}).get("rule_text") or "আজ নতুন কিছু শিখব।"
        insight = result.get("insight_bn") or ""
        question = result.get("question_bn") or ""
        lines = [
            "বস, আজকের Thinker চ্যালেঞ্জ 🧠",
            f"📌 শেখা: {rule}",
        ]
        if insight:
            lines.append(f"💡 উপলব্ধি: {insight}")
        if question:
            lines.append(f"❓ প্রশ্ন: {question}")
        return "\n".join(lines)

    def _intruder_result(self) -> Dict[str, Any]:
        security = self.security or SecurityGuard(settings=self.settings)
        self.security = security
        try:
            return security.intruder_check()
        except Exception as exc:
            log.warning("Intruder check failed: %s", exc)
            return {"ok": False, "summary_bn": "বস, ঘরের চেক করতে পারলাম না।", "error": str(exc)}

    def _receptionist_text(self, argument: str) -> str:
        if self.receptionist is None:
            return "বস, রিসেপশনিস্ট মডিউল এখন সংযুক্ত নেই।"
        wanted = (argument or "").strip().lower()
        if wanted in ("on", "চালু", "start", "1", "true"):
            self.receptionist.auto_reception = True
            return "বস, অটো-রিসেপশনিস্ট চালু করেছি ✅ — কল এলে আমি ধরব।"
        if wanted in ("off", "বন্ধ", "stop", "0", "false"):
            self.receptionist.auto_reception = False
            return "বস, অটো-রিসেপশনিস্ট বন্ধ করেছি ⏸️ — কল এখন আপনি ধরবেন।"
        state = "চালু ✅" if getattr(self.receptionist, "auto_reception", False) else "বন্ধ ⏸️"
        return f"বস, রিসেপশনিস্ট এখন {state}। বদলাতে `/receptionist on` বা `/receptionist off` লিখুন।"

    def handle_command(self, text: str, chat_id: Any) -> Dict[str, Any]:
        head, _, argument = text.partition(" ")
        command = head.split("@", 1)[0].lower()  # /help@OrbitBot -> /help
        kind = COMMANDS.get(command)

        if kind == "welcome":
            reply, payload = WELCOME_BN, {}
        elif kind == "status":
            reply, payload = self._status_text(), {}
        elif kind == "briefing":
            reply, payload = self._briefing_text(), {}
        elif kind == "thinker":
            reply, payload = self._thinker_text(), {}
        elif kind == "intruder":
            result = self._intruder_result()
            self.send_intruder_alert(result)
            return {
                "ok": bool(result.get("ok")),
                "kind": "command",
                "command": command,
                "reply_bn": self._intruder_caption(result),
                "photo": bool(result.get("photo_path")),
                "result": result,
            }
        elif kind == "receptionist":
            reply, payload = self._receptionist_text(argument), {}
        else:
            reply, payload = UNKNOWN_COMMAND_BN, {}

        sent = self.send_message(chat_id, reply)
        return {
            "ok": bool(sent.get("ok")),
            "kind": "command",
            "command": command,
            "reply_bn": reply,
            "sent": sent,
            **payload,
        }

    # ------------------------------------------------------------------ #
    # Free text + voice
    # ------------------------------------------------------------------ #

    def _dispatcher(self) -> Any:
        if self.dispatcher is None:
            self.dispatcher = ActionDispatcher(
                settings=self.settings,
                memory=self.memory,
                voice=self.voice,
            )
        return self.dispatcher

    def _reply_text(self, outcome: Dict[str, Any]) -> str:
        reply = str(
            outcome.get("reply_bn")
            or outcome.get("summary_bn")
            or "বস, কাজটা শেষ হয়েছে।"
        ).strip()
        result = outcome.get("result") or {}
        if isinstance(result, dict):
            maps = result.get("maps_url")
            if maps and maps not in reply:
                reply = f"{reply}\n\n📍 {maps}"
            description = result.get("description_bn")
            if outcome.get("action") == "read_screen" and description and description not in reply:
                reply = f"{reply}\n\n{description}"
        return reply

    def handle_text(self, text: str, chat_id: Any) -> Dict[str, Any]:
        if not text.strip():
            self.send_message(chat_id, EMPTY_TEXT_BN)
            return {"ok": False, "kind": "text", "error": "empty text"}

        try:
            outcome = self._dispatcher().process(text)
        except Exception as exc:  # dispatcher already guards, this is belt+braces
            log.exception("Dispatcher crashed from Telegram")
            outcome = {
                "ok": False,
                "reply_bn": "বস, এই কাজটা করতে গিয়ে সমস্যা হয়েছে।",
                "error": str(exc),
            }

        reply = self._reply_text(outcome)
        result = outcome.get("result") or {}
        photo = result.get("photo_path") if isinstance(result, dict) else None
        if outcome.get("action") == "intruder_check" and photo and Path(str(photo)).is_file():
            sent = self.send_photo(chat_id, Path(str(photo)), caption=reply[:1024])
        else:
            sent = self.send_message(chat_id, reply)

        if self.reply_voice:
            self._send_voice_reply(chat_id, reply)
        return {
            "ok": bool(outcome.get("ok")),
            "kind": "text",
            "reply_bn": reply,
            "action": outcome.get("action"),
            "sent": sent,
        }

    def _send_voice_reply(self, chat_id: Any, reply: str) -> Optional[Dict[str, Any]]:
        if self.voice is None:
            return None
        try:
            audio = self.voice.synthesize(reply)
        except Exception as exc:
            log.warning("Voice reply synthesis failed: %s", exc)
            return None
        if not audio:
            return None
        return self.send_voice(chat_id, Path(audio))

    def transcribe(self, audio: Path) -> str:
        """Use ``transcribe_file`` when the engine exposes it, else ``transcribe``."""
        engine = self.voice
        if engine is None:
            from voice_engine import VoiceEngine

            engine = VoiceEngine(settings=self.settings)
            self.voice = engine
        function = getattr(engine, "transcribe_file", None) or getattr(engine, "transcribe", None)
        if function is None:
            raise RuntimeError("voice engine has no transcription method")
        return str(function(Path(audio)) or "").strip()

    def download_file(self, file_id: str, suffix: str = ".ogg") -> Optional[Path]:
        info = self.api_call("getFile", {"file_id": file_id})
        if not info.get("ok"):
            return None
        remote = (info.get("result") or {}).get("file_path")
        if not remote or self.session is None:
            self.last_error = "getFile returned no file_path"
            return None
        try:
            response = self.session.get(
                FILE_ROOT.format(token=self.token, path=remote), timeout=self.request_timeout
            )
            content = getattr(response, "content", b"") or b""
            if getattr(response, "status_code", 0) != 200 or not content:
                self.last_error = f"file download failed: HTTP {getattr(response, 'status_code', '?')}"
                return None
            self.audio_dir.mkdir(parents=True, exist_ok=True)
            extension = Path(remote).suffix or suffix
            target = self.audio_dir / f"tg_{int(self.clock() * 1000)}{extension}"
            target.write_bytes(content)
            return target
        except Exception as exc:
            self.last_error = f"file download error: {exc}"
            log.warning("Telegram file download failed: %s", exc)
            return None

    def handle_voice(self, message: Dict[str, Any], chat_id: Any) -> Dict[str, Any]:
        media = message.get("voice") or message.get("audio") or {}
        file_id = media.get("file_id")
        if not file_id:
            self.send_message(chat_id, DOWNLOAD_FAIL_BN)
            return {"ok": False, "kind": "voice", "error": "no file_id"}

        audio = self.download_file(str(file_id))
        if audio is None:
            self.send_message(chat_id, DOWNLOAD_FAIL_BN)
            return {"ok": False, "kind": "voice", "error": self.last_error or "download failed"}

        try:
            transcript = self.transcribe(audio)
        except Exception as exc:
            log.warning("Transcription failed: %s", exc)
            self.send_message(chat_id, TRANSCRIBE_FAIL_BN)
            return {"ok": False, "kind": "voice", "error": str(exc)}

        if not transcript:
            self.send_message(chat_id, TRANSCRIBE_FAIL_BN)
            return {"ok": False, "kind": "voice", "error": "empty transcript"}

        self.send_message(chat_id, f"🎧 শুনলাম: {transcript}")
        result = self.handle_text(transcript, chat_id)
        result.update({"kind": "voice", "transcript": transcript})
        return result

    # ------------------------------------------------------------------ #
    # Update handling
    # ------------------------------------------------------------------ #

    def handle_update(self, update: Dict[str, Any]) -> Dict[str, Any]:
        self.last_update = update
        message = update.get("message") or update.get("edited_message") or {}
        if not isinstance(message, dict) or not message:
            return {"ok": False, "kind": "ignored", "error": "no message"}

        chat_id = (message.get("chat") or {}).get("id")
        if not self.is_authorized(chat_id):
            self._refuse(chat_id, message)
            return {
                "ok": False,
                "authorized": False,
                "kind": "refused",
                "chat_id": chat_id,
            }

        self.updates_handled += 1
        text = str(message.get("text") or "").strip()

        if text.startswith("/"):
            result = self.handle_command(text, chat_id)
        elif message.get("voice") or message.get("audio"):
            result = self.handle_voice(message, chat_id)
        elif text:
            result = self.handle_text(text, chat_id)
        else:
            self.send_message(chat_id, EMPTY_TEXT_BN)
            result = {"ok": False, "kind": "ignored", "error": "empty message"}

        result["authorized"] = True
        return result

    def _refuse(self, chat_id: Any, message: Dict[str, Any]) -> None:
        log.warning(
            "SECURITY: unauthorized Telegram chat_id=%s sender=%s — command refused without execution",
            chat_id,
            (message.get("from") or {}).get("id"),
        )
        try:
            key = int(chat_id)
        except (TypeError, ValueError):
            key = str(chat_id)
        if key in self._refused:
            return
        self._refused.add(key)
        self.send_message(chat_id, REFUSAL_BN)

    # ------------------------------------------------------------------ #
    # Polling
    # ------------------------------------------------------------------ #

    def get_updates(
        self,
        offset: Optional[int] = None,
        timeout: Optional[int] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        return self.api_call(
            "getUpdates",
            {
                "offset": offset if offset is not None else self._offset,
                "timeout": self.poll_timeout if timeout is None else int(timeout),
                "limit": max(1, min(int(limit), 100)),
                "allowed_updates": ["message"],
            },
        )

    def poll_once(self, limit: int = 20) -> Dict[str, Any]:
        """Fetch one batch, handle every update, advance the offset."""
        response = self.get_updates(limit=limit)
        if not response.get("ok"):
            return {"ok": False, "error": response.get("error") or self.last_error, "handled": []}

        updates = response.get("result") or []
        if not isinstance(updates, list):
            updates = []

        handled: List[Dict[str, Any]] = []
        for update in updates:
            if not isinstance(update, dict):
                continue
            try:
                handled.append(self.handle_update(update))
            except Exception as exc:  # one bad update must not kill polling
                log.exception("Update handling crashed")
                handled.append({"ok": False, "kind": "error", "error": str(exc)})
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._offset = update_id + 1
        return {"ok": True, "count": len(updates), "handled": handled, "offset": self._offset}

    def poll_forever(
        self,
        stop_event: Any = None,
        max_polls: Optional[int] = None,
        max_seconds: Optional[float] = None,
        on_update: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """Blocking long-poll loop for the daemon thread. Returns when stopped."""
        started = self.clock()
        polls = 0
        handled = 0
        while True:
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                break
            if max_polls is not None and polls >= max_polls:
                break
            if max_seconds is not None and (self.clock() - started) >= max_seconds:
                break

            result = self.poll_once()
            polls += 1
            if result.get("ok"):
                for item in result.get("handled") or []:
                    handled += 1
                    if on_update is not None:
                        try:
                            on_update(item)
                        except Exception:
                            log.exception("on_update callback failed")
            else:
                log.warning("Telegram poll failed: %s", result.get("error"))
                if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                    break
                self.sleep(self.backoff)

        return {"ok": True, "polls": polls, "handled": handled, "updates_handled": self.updates_handled}

    # ------------------------------------------------------------------ #
    # Diagnostics (main.py doctor)
    # ------------------------------------------------------------------ #

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "ok": bool(self.token),
            "token": bool(self.token),
            "admin_chat_id": self.admin_chat_id,
            "authorized_only": self.admin_chat_id is not None,
            "polling": self.updates_handled > 0,
            "reply_voice": self.reply_voice,
            "last_error": self.last_error,
        }


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    controller = TelegramController()
    print(controller.diagnostics())
    if controller.admin_chat_id is not None:
        print(controller.notify_boss("বস, Orbit অনলাইন।"))
    controller.poll_forever()
