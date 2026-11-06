"""
notification_responder.py — ORBIT reads the shade and answers for Boss.

    from notification_responder import NotificationResponder
    nr = NotificationResponder(auto_reply=False)
    nr.poll()                     # -> new WhatsApp/Telegram/SMS messages, logged
    nr.reply_to(message, "আসছি")  # -> WhatsApp deep link / SMS
    nr.poll(auto_reply=True)      # -> brain drafts a polite Bengali answer

Sources:

* ``termux-notification-list`` (JSON) for active notifications,
* ``memory_engine`` so "হোয়াটসঅ্যাপে কে মেসেজ দিয়েছে?" is answerable later,
* ``adb_automation.send_whatsapp`` for WhatsApp (the deep link carries Bengali
  text correctly — ``adb shell input text`` cannot),
* ``comm_manager.send_sms`` for the Messages app,
* an optional ADB UI-tap "inline reply" for plain-ASCII text (Termux cannot
  execute another app's RemoteInput action directly, so this is opt-in).

Every method returns ``{"ok": bool, ...}`` and never raises; missing
``termux-notification-list``, bad JSON or a locked database all become an
error dict with a Bengali message.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from adb_automation import AdbAutomation
from comm_manager import CommManager
from config import settings as default_settings
from system_control import CommandResult, SystemControl, run_command, to_bengali_digits

log = logging.getLogger("orbit.notifications")

NOTIFICATION_TIMEOUT = 20.0
DEFAULT_ADB = ("adb", "shell")
SEEN_LIMIT = 200
REPLY_CHAR_LIMIT = 320

# package -> Bengali app name used in summaries and memory text
MESSAGING_APPS: Dict[str, str] = {
    "com.whatsapp": "হোয়াটসঅ্যাপ",
    "com.whatsapp.w4b": "হোয়াটসঅ্যাপ বিজনেস",
    "org.telegram.messenger": "টেলিগ্রাম",
    "org.thunderdog.challegram": "টেলিগ্রাম",
    "com.google.android.apps.messaging": "মেসেজ",
    "com.android.mms": "মেসেজ",
    "com.samsung.android.messaging": "মেসেজ",
    "com.oneplus.mms": "মেসেজ",
    "com.miui.smsextra": "মেসেজ",
}

SMS_PACKAGES = {
    "com.google.android.apps.messaging",
    "com.android.mms",
    "com.samsung.android.messaging",
    "com.oneplus.mms",
    "com.miui.smsextra",
}

WHATSAPP_PACKAGES = {"com.whatsapp", "com.whatsapp.w4b"}
TELEGRAM_PACKAGES = {"org.telegram.messenger", "org.thunderdog.challegram"}

# UI text that opens an inline reply box (uiautomator dump matching).
REPLY_WORDS = ("reply", "উত্তর", "রিপ্লাই", "answer", "prompt")

REPLY_PROMPT = (
    "তুমি বসের পক্ষ থেকে তার ফোনে আসা একটা মেসেজের উত্তর লিখছ। "
    "মেসেজটা কে পাঠিয়েছে: {sender} ({app})। "
    "তার লেখা: \"{text}\"\n"
    "বসের পক্ষ থেকে ছোট, ভদ্র, স্বাভাবিক বাংলায় একটি উত্তর লেখো (সর্বোচ্চ দুই বাক্য)। "
    "শুধু উত্তরটাই লেখো, আগে 'বস' সম্বোধন করো না, কোনো ব্যাখ্যা নয়।"
)


def _loads_json(text: str) -> Any:
    """Parse JSON even when Termux prints a warning line before it."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty output")
    try:
        return json.loads(raw)
    except Exception:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = raw.find(opener), raw.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except Exception:
                continue
    raise ValueError(f"no JSON in output: {raw[:120]!r}")


class NotificationResponder:
    """Polls the notification shade, logs messages and (optionally) answers."""

    def __init__(
        self,
        brain: Any = None,
        memory: Any = None,
        adb: Optional[AdbAutomation] = None,
        comm: Optional[CommManager] = None,
        control: Optional[SystemControl] = None,
        runner: Any = run_command,
        settings: Optional[Any] = None,
        auto_reply: bool = False,
        auto_send: bool = True,
        use_inline: bool = False,
        seen_limit: int = SEEN_LIMIT,
        adb_prefix: Sequence[str] = DEFAULT_ADB,
        timeout: float = NOTIFICATION_TIMEOUT,
    ) -> None:
        self.settings = settings or default_settings
        self.brain = brain
        self.memory = memory
        self.control = control or SystemControl(settings=self.settings)
        self.adb = adb or AdbAutomation(control=self.control, settings=self.settings)
        self.comm = comm or CommManager(control=self.control, settings=self.settings)
        self._runner = runner
        self.auto_reply = bool(auto_reply)
        self.auto_send = bool(auto_send)
        self.use_inline = bool(use_inline)
        self.seen_limit = max(10, int(seen_limit))
        self.adb_prefix = tuple(adb_prefix)
        self.timeout = float(timeout)
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self.last_error: Optional[str] = None

    # -- plumbing ---------------------------------------------------------- #

    def _run(self, cmd: Sequence[str], timeout: Optional[float] = None) -> CommandResult:
        try:
            return self._runner(list(cmd), timeout or self.timeout)
        except TypeError:
            return self._runner(list(cmd))

    def _adb(self, *args: str) -> List[str]:
        return [*self.adb_prefix, *args]

    def _fail(self, error: str, action: str = "notifications", **extra: Any) -> Dict[str, Any]:
        self.last_error = error
        log.warning("%s: %s", action, error)
        return {"ok": False, "action": action, "error": error, **extra}

    # ------------------------------------------------------------------ #
    # Reading the shade
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalise(raw: Dict[str, Any]) -> Dict[str, Any]:
        package = str(raw.get("packageName") or raw.get("package") or "").strip()
        app_bn = MESSAGING_APPS.get(package) or str(raw.get("appName") or package or "অ্যাপ")
        text = str(raw.get("content") or raw.get("text") or raw.get("message") or "").strip()
        title = str(raw.get("title") or raw.get("sender") or "").strip()
        when = raw.get("when") or raw.get("timestamp") or 0
        identifier = str(raw.get("id") or "")
        key = str(raw.get("key") or "").strip() or f"{package}:{identifier}"
        return {
            "key": key,
            "id": identifier,
            "package": package,
            "app": app_bn,
            "app_name": str(raw.get("appName") or package),
            "title": title,
            "text": text,
            "when": when,
            "category": str(raw.get("category") or ""),
        }

    def list_notifications(self) -> Dict[str, Any]:
        """All active notifications (not yet filtered)."""
        result = self._run(["termux-notification-list"], timeout=self.timeout)
        if not result.ok:
            return self._fail(result.describe(), "notifications")

        try:
            data = _loads_json(result.output)
        except ValueError as exc:
            return self._fail(f"unreadable notification list: {exc}", "notifications")

        if isinstance(data, dict):
            data = data.get("notifications") or data.get("data") or []
        if not isinstance(data, list):
            return self._fail("notification list is not a JSON array", "notifications")

        items = [self._normalise(item) for item in data if isinstance(item, dict)]
        return {"ok": True, "notifications": items, "count": len(items)}

    @staticmethod
    def is_messaging(item: Dict[str, Any]) -> bool:
        package = item.get("package") or ""
        if package in MESSAGING_APPS:
            return True
        if any(package.startswith(prefix + ".") for prefix in WHATSAPP_PACKAGES | TELEGRAM_PACKAGES):
            return True
        category = (item.get("category") or "").lower()
        return category in ("msg", "message", "im")

    def _mark_seen(self, key: str) -> None:
        self._seen[key] = None
        self._seen.move_to_end(key)
        while len(self._seen) > self.seen_limit:
            self._seen.popitem(last=False)

    @staticmethod
    def notification_key(item: Dict[str, Any]) -> str:
        """Stable identity: Termux's key, else package+id, else content hash."""
        key = str(item.get("key") or "").strip()
        if key:
            return key
        package = item.get("package") or ""
        identifier = str(item.get("id") or "").strip()
        if identifier:
            return f"{package}:{identifier}"
        return "|".join(
            str(item.get(field) or "") for field in ("package", "title", "text", "when")
        )

    # ------------------------------------------------------------------ #
    # Memory
    # ------------------------------------------------------------------ #

    def log_messages(self, messages: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        """Store each message as an episode so Boss can ask about it later."""
        logged: List[Dict[str, Any]] = []
        failed: List[str] = []

        for message in messages:
            summary_bn = self.describe_message(message)
            user_text = summary_bn
            decision = {
                "action": "notification",
                "params": {
                    "app": message.get("app"),
                    "package": message.get("package"),
                    "sender": message.get("title"),
                    "text": message.get("text"),
                },
                "reply_bn": summary_bn,
            }
            if self.memory is None:
                continue
            try:
                saved = self.memory.remember_turn(user_text, decision, outcome="notification")
                logged.append({"key": message.get("key"), "episode_id": saved.get("episode_id")})
            except Exception as exc:
                log.warning("Could not log notification to memory: %s", exc)
                failed.append(str(exc))

        return {"ok": True, "logged": logged, "count": len(logged), "errors": failed}

    @staticmethod
    def describe_message(message: Dict[str, Any]) -> str:
        sender = (message.get("title") or "কেউ").strip()
        text = (message.get("text") or "").strip()
        app = message.get("app") or "মেসেজ"
        if text:
            return f"{app} থেকে {sender} লিখেছেন: {text}"
        return f"{app} থেকে {sender} একটি নোটিফিকেশন পাঠিয়েছেন।"

    # ------------------------------------------------------------------ #
    # Polling
    # ------------------------------------------------------------------ #

    def _message(self, item: Dict[str, Any]) -> Dict[str, Any]:
        message = dict(item)
        message["sender"] = item.get("title") or ""
        message["display_bn"] = self.describe_message(item)
        return message

    def poll(self, auto_reply: Optional[bool] = None, log: bool = True) -> Dict[str, Any]:
        """Return only messaging notifications not seen in an earlier poll."""
        listing = self.list_notifications()
        if not listing.get("ok"):
            return {
                "ok": False,
                "action": "notifications",
                "error": listing.get("error"),
                "new_messages": [],
                "count": 0,
                "replies": [],
                "summary_bn": "বস, নোটিফিকেশন পড়া গেল না।",
            }

        fresh: List[Dict[str, Any]] = []
        for item in listing["notifications"]:
            if not self.is_messaging(item):
                continue
            key = self.notification_key(item)
            if key in self._seen:
                continue
            self._mark_seen(key)
            fresh.append(self._message(item))

        logged = self.log_messages(fresh) if (fresh and log) else {"ok": True, "logged": [], "count": 0, "errors": []}

        replies: List[Dict[str, Any]] = []
        wanted = self.auto_reply if auto_reply is None else bool(auto_reply)
        if wanted:
            for message in fresh:
                drafted = self.compose_reply(message)
                if drafted.get("ok"):
                    replies.append(self.reply_to(message, drafted["text"]))
                else:
                    replies.append(
                        {
                            "ok": False,
                            "message_key": message.get("key"),
                            "error": drafted.get("error"),
                            "needs_manual": True,
                        }
                    )

        count = len(fresh)
        if count:
            senders = "".join(sorted({f" {message['title'] or 'অজানা'}" for message in fresh}))[:120]
            summary_bn = f"বস, {to_bengali_digits(count)}টা নতুন মেসেজ এসেছে —{senders} থেকে।"
        else:
            summary_bn = "বস, নতুন কোনো মেসেজ নেই।"

        return {
            "ok": True,
            "action": "notifications",
            "new_messages": fresh,
            "count": count,
            "logged": logged.get("logged", []),
            "replies": replies,
            "auto_reply": wanted,
            "summary_bn": summary_bn,
        }

    # ------------------------------------------------------------------ #
    # Replying
    # ------------------------------------------------------------------ #

    def compose_reply(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Ask the brain for a short Bengali reply in Boss's voice."""
        if self.brain is None:
            return self._fail("no brain available for auto-reply", "notifications")
        prompt = REPLY_PROMPT.format(
            sender=message.get("title") or "অজানা",
            app=message.get("app") or "মেসেজ",
            text=(message.get("text") or "")[:300],
        )
        try:
            decision = self.brain.think(prompt)
        except Exception as exc:
            return self._fail(f"brain failed drafting a reply: {exc}", "notifications")

        body = ""
        action = str(decision.get("action") or "")
        params = decision.get("params") or {}
        if action in ("sms", "whatsapp", "speak") and isinstance(params, dict):
            body = str(params.get("message") or params.get("text") or "").strip()
        body = body or str(decision.get("reply_bn") or "").strip()

        for prefix in ("বস, ", "বস,"):
            if body.startswith(prefix):
                body = body[len(prefix):].strip()
        body = body[:REPLY_CHAR_LIMIT].strip()
        if not body:
            return self._fail("the brain returned an empty reply", "notifications")
        return {"ok": True, "text": body, "draft": body}

    def _reply_whatsapp(self, message: Dict[str, Any], text: str) -> Dict[str, Any]:
        result = self.adb.send_whatsapp(
            contact=message.get("title"),
            message=text,
            press_send=self.auto_send,
        )
        result["via"] = "whatsapp"
        result["message_key"] = message.get("key")
        return result

    def _reply_sms(self, message: Dict[str, Any], text: str) -> Dict[str, Any]:
        result = self.comm.send_sms(contact=message.get("title"), message=text)
        result["via"] = "sms"
        result["message_key"] = message.get("key")
        return result

    def reply_to(
        self,
        message: Dict[str, Any],
        text: Optional[str] = None,
        generate: bool = False,
    ) -> Dict[str, Any]:
        """Send a reply on the same channel the message arrived on."""
        body = str(text or "").strip()
        if not body and generate:
            drafted = self.compose_reply(message)
            if not drafted.get("ok"):
                return drafted
            body = drafted["text"]
        if not body:
            return self._fail("কী উত্তর দেব সেটা বলেননি", "reply", message_key=message.get("key"))

        package = message.get("package") or ""
        attempts: List[Dict[str, Any]] = []

        if self.use_inline:
            inline = self.inline_reply(body)
            attempts.append({"via": "inline", **inline})
            if inline.get("ok"):
                return {
                    "ok": True,
                    "action": "reply",
                    "via": "inline",
                    "message_key": message.get("key"),
                    "text": body,
                    "attempts": attempts,
                }

        if package in WHATSAPP_PACKAGES or package.startswith("com.whatsapp."):
            result = self._reply_whatsapp(message, body)
        elif package in SMS_PACKAGES:
            result = self._reply_sms(message, body)
        else:
            app = message.get("app") or package or "এই অ্যাপ"
            result = {
                "ok": False,
                "action": "reply",
                "needs_manual": True,
                "message_key": message.get("key"),
                "text": body,
                "error": f"{app}-এ অটো রিপ্লাই পাঠানো যায় না",
                "message_bn": f"বস, {app}-এর উত্তরটা আপনাকে নিজে পাঠাতে হবে।",
            }

        result.setdefault("action", "reply")
        result["attempts"] = attempts + list(result.get("attempts") or [])
        return result

    # ------------------------------------------------------------------ #
    # Optional inline reply (ASCII only)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _ascii_only(text: str) -> bool:
        return all(ord(char) < 128 for char in text)

    @staticmethod
    def _reply_bounds(xml_text: str) -> Optional[Tuple[int, int]]:
        """Centre of the first Reply node in a uiautomator dump."""
        try:
            root = ET.fromstring(xml_text)
        except Exception:
            return None
        fallback: Optional[Tuple[int, int]] = None
        for node in root.iter("node"):
            label = f"{node.get('text') or ''} {node.get('content-desc') or ''}".strip().lower()
            if not label or not any(word in label for word in REPLY_WORDS):
                continue
            bounds = node.get("bounds") or ""
            match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
            if not match:
                continue
            x1, y1, x2, y2 = (int(value) for value in match.groups())
            centre = ((x1 + x2) // 2, (y1 + y2) // 2)
            # An exact short label ("Reply") beats a paragraph that mentions it.
            if label in REPLY_WORDS:
                return centre
            fallback = fallback or centre
        return fallback

    def inline_reply(self, text: str) -> Dict[str, Any]:
        """Open the shade, tap Reply, type and send (ASCII text only)."""
        body = str(text or "").strip()
        if not body:
            return {"ok": False, "error": "empty reply"}
        if not self._ascii_only(body):
            return {
                "ok": False,
                "skipped": "non-ascii",
                "error": "adb input cannot type Bengali — using the app's deep link instead",
            }

        self._run(self._adb("cmd", "statusbar", "expand-notifications"))
        dumped = self._run(self._adb("uiautomator", "dump", "/sdcard/orbit_ui.xml"))
        local = Path(tempfile.mkstemp(prefix="orbit_ui_", suffix=".xml")[1])
        pulled = self._run(["adb", "pull", "/sdcard/orbit_ui.xml", str(local)])
        try:
            if not dumped.ok or not pulled.ok:
                return {"ok": False, "error": "could not dump the notification shade"}
            bounds = self._reply_bounds(local.read_text(encoding="utf-8", errors="ignore"))
        finally:
            local.unlink(missing_ok=True)

        if bounds is None:
            return {"ok": False, "error": "no Reply button found in the shade"}

        self._run(self._adb("input", "tap", str(bounds[0]), str(bounds[1])))
        self._run(self._adb("input", "text", body.replace(" ", "%s")))
        sent = self._run(self._adb("input", "keyevent", "66"))
        return {
            "ok": sent.ok,
            "detail": f"tap {bounds[0]},{bounds[1]} + input text + KEYCODE_ENTER",
            "bounds": bounds,
            "error": None if sent.ok else sent.describe(),
        }

    # ------------------------------------------------------------------ #
    # Long-running watch
    # ------------------------------------------------------------------ #

    def watch(
        self,
        interval: float = 30.0,
        max_seconds: Optional[float] = None,
        stop_event: Any = None,
    ) -> Dict[str, Any]:
        started = time.monotonic()
        polls = 0
        all_new: List[Dict[str, Any]] = []
        while True:
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                break
            result = self.poll()
            polls += 1
            all_new.extend(result.get("new_messages") or [])
            if max_seconds is not None and (time.monotonic() - started) >= max_seconds:
                break
            time.sleep(max(1.0, float(interval)))
        return {"ok": True, "polls": polls, "new_messages": all_new}


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    responder = NotificationResponder()
    print(json.dumps(responder.poll(), ensure_ascii=False, indent=2)[:2000])
