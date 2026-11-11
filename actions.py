"""
actions.py — one place that turns a brain decision into a real action.

``brain.think()`` only *decides*; ``ActionDispatcher`` is what actually calls
Termux, ADB, the briefing agent or the receptionist, then speaks the Bengali
outcome and records it.

    from actions import ActionDispatcher
    dispatcher = ActionDispatcher()
    dispatcher.execute({"action": "torch", "params": {"state": "on"},
                        "reply_bn": "বস, টর্চ জ্বালাচ্ছি।"})

    dispatcher.process("আজকের বাজার কেমন?")      # brain -> dispatch -> speak

Every one of the 13 whitelisted actions is routed:

    call, answer_call, sms, whatsapp, torch, battery, volume, wifi,
    intruder_check, open_app, read_screen, market_news, speak

The final ``reply_bn`` is always addressed to Boss. It prefers what the module
actually reported (a failure never gets the model's optimistic line spoken),
and falls back to the decision's own reply. Failures never raise.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from adb_automation import AdbAutomation
from brain import SUPPORTED_ACTIONS, ensure_boss_address
from briefing_agent import BriefingAgent
from call_receptionist import CallReceptionist
from comm_manager import CommManager
from config import settings as default_settings
from pc_automation import PcAutomation
from security_guard import SecurityGuard
from system_control import SystemControl

log = logging.getLogger("orbit.actions")

ACTION_NAMES_BN: Dict[str, str] = {
    "call": "কল",
    "answer_call": "কল ধরা",
    "sms": "মেসেজ পাঠানো",
    "whatsapp": "হোয়াটসঅ্যাপে মেসেজ",
    "torch": "টর্চ",
    "battery": "ব্যাটারি দেখা",
    "volume": "ভলিউম বদলানো",
    "wifi": "ওয়াই-ফাই",
    "intruder_check": "ঘরের চেক",
    "open_app": "অ্যাপ খোলা",
    "open_website": "ওয়েবসাইট খোলা",
    "open_folder": "ফোল্ডার খোলা",
    "pc_health": "কম্পিউটারের স্বাস্থ্য",
    "read_screen": "স্ক্রিন পড়া",
    "market_news": "বাজার ও খবর",
    "speak": "বলা",
}

UNKNOWN_ACTION_BN = "বস, এই কাজটা আমি চিনি না, তাই কিছু করলাম না।"


class ActionDispatcher:
    """Routes validated decisions to modules, then speaks and remembers."""

    def __init__(
        self,
        control: Optional[SystemControl] = None,
        comm: Optional[CommManager] = None,
        adb: Optional[AdbAutomation] = None,
        briefing: Optional[BriefingAgent] = None,
        security: Optional[SecurityGuard] = None,
        receptionist: Optional[CallReceptionist] = None,
        pc: Optional[PcAutomation] = None,
        brain: Any = None,
        voice: Any = None,
        memory: Any = None,
        settings: Optional[Any] = None,
        speak_reply: bool = True,
        record: bool = True,
        auto_send_whatsapp: bool = False,
    ) -> None:
        self.settings = settings or default_settings
        self.control = control or SystemControl(settings=self.settings)
        self.pc = pc or PcAutomation(settings=self.settings)
        self.comm = comm or CommManager(control=self.control, settings=self.settings)
        self.adb = adb or AdbAutomation(control=self.control, settings=self.settings)
        self.briefing = briefing or BriefingAgent(settings=self.settings)
        self.security = security or SecurityGuard(settings=self.settings)
        self.receptionist = receptionist or CallReceptionist(
            brain=brain, settings=self.settings
        )
        self.brain = brain
        self.voice = voice
        self.memory = memory
        self.speak_reply = bool(speak_reply)
        self.record = bool(record)
        self.auto_send_whatsapp = bool(auto_send_whatsapp)
        self.last_result: Optional[Dict[str, Any]] = None
        self.history: list[Dict[str, Any]] = []

    # -- lazy collaborators ------------------------------------------------ #

    def _resolve_voice(self) -> Any:
        if self.voice is False:
            return None
        if self.voice is None:
            try:
                from voice_engine import VoiceEngine

                self.voice = VoiceEngine(settings=self.settings)
            except Exception as exc:
                log.warning("Voice engine unavailable: %s", exc)
                self.voice = False
        return self.voice or None

    def _resolve_memory(self) -> Any:
        if self.memory is None:
            try:
                from memory_engine import get_memory

                self.memory = get_memory()
            except Exception as exc:
                log.warning("Memory unavailable: %s", exc)
                return None
        return self.memory

    # ------------------------------------------------------------------ #
    # Handlers: one per whitelisted action
    # ------------------------------------------------------------------ #

    def _call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.comm.make_call(
            contact=params.get("contact"), number=params.get("number")
        )

    def _answer_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.receptionist.answer()

    def _sms(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.comm.send_sms(
            contact=params.get("contact"),
            number=params.get("number"),
            message=params.get("message", ""),
        )

    def _whatsapp(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.adb.send_whatsapp(
            contact=params.get("contact"),
            number=params.get("number"),
            message=params.get("message", ""),
            press_send=self.auto_send_whatsapp,
        )

    def _torch(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.control.torch(params.get("state", "on"))

    def _battery(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.control.battery_status()

    def _volume(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.control.set_volume(
            level=params.get("level"),
            direction=params.get("direction"),
            stream=params.get("stream") or "music",
        )

    def _wifi(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.control.toggle_wifi(params.get("state", "on"))

    def _intruder_check(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.security.intruder_check(capture=bool(params.get("capture", True)))

    def _open_app(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.pc.open_app(params.get("app") or params.get("app_name") or "")

    def _open_website(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.pc.open_website(params.get("url") or params.get("website") or "")

    def _open_folder(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.pc.open_folder(params.get("path") or params.get("folder") or "")

    def _pc_health(self, params: Dict[str, Any]) -> Dict[str, Any]:
        health = self.pc.system_health()
        if health.get("ok") and not health.get("reply_bn"):
            health["reply_bn"] = health.get("summary_bn")
        return health

    def _read_screen(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.adb.read_screen(prompt_bn=params.get("prompt_bn") or "স্ক্রিনে কী আছে সংক্ষেপে বলো")

    def _market_news(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.briefing.market_news(topic=params.get("topic") or "general")

    def _speak_action(self, params: Dict[str, Any]) -> Dict[str, Any]:
        text = str(params.get("text") or "").strip()
        if not text:
            return {
                "ok": False,
                "action": "speak",
                "error": "nothing to say",
                "message_bn": "বস, কী বলব সেটা পেলাম না।",
            }
        return {"ok": True, "action": "speak", "text": text, "reply_bn": text}

    def handlers(self) -> Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]]:
        return {
            "call": self._call,
            "answer_call": self._answer_call,
            "sms": self._sms,
            "whatsapp": self._whatsapp,
            "torch": self._torch,
            "battery": self._battery,
            "volume": self._volume,
            "wifi": self._wifi,
            "intruder_check": self._intruder_check,
            "open_app": self._open_app,
            "open_website": self._open_website,
            "open_folder": self._open_folder,
            "pc_health": self._pc_health,
            "read_screen": self._read_screen,
            "market_news": self._market_news,
            "speak": self._speak_action,
        }

    # ------------------------------------------------------------------ #
    # Reply text
    # ------------------------------------------------------------------ #

    @staticmethod
    def _reply_text(result: Dict[str, Any], decision: Dict[str, Any], action: str) -> str:
        """Prefer what actually happened; never speak a false success."""
        if result.get("ok"):
            for key in ("reply_bn", "summary_bn", "message_bn", "text"):
                value = str(result.get(key) or "").strip()
                if value:
                    return ensure_boss_address(value)
            fallback = str(decision.get("reply_bn") or "").strip()
            return ensure_boss_address(fallback or f"বস, {ACTION_NAMES_BN.get(action, 'কাজ')} শেষ হয়েছে।")

        for key in ("message_bn", "reply_bn", "summary_bn"):
            value = str(result.get(key) or "").strip()
            if value:
                return ensure_boss_address(value)
        name = ACTION_NAMES_BN.get(action, "কাজ")
        return f"বস, {name}টা করতে পারলাম না।"

    # ------------------------------------------------------------------ #
    # Speak + remember
    # ------------------------------------------------------------------ #

    def _say(self, text: str) -> Dict[str, Any]:
        voice = self._resolve_voice()
        if voice is None:
            return {"spoken": False, "engine": None}
        try:
            spoken = bool(voice.speak(text))
        except Exception as exc:
            log.warning("TTS failed in dispatcher: %s", exc)
            return {"spoken": False, "engine": None, "error": str(exc)}
        engine = getattr(voice, "last_tts_engine", None)
        return {"spoken": spoken, "engine": engine}

    def _remember(
        self,
        user_text: str,
        decision: Dict[str, Any],
        result: Dict[str, Any],
        action: str,
        ok: bool,
    ) -> Dict[str, Any]:
        engine = self._resolve_memory()
        if engine is None:
            return {"ok": False, "skipped": "no memory"}
        compact = {
            "action": action,
            "params": decision.get("params") or {},
            "reply_bn": result.get("reply_bn") or decision.get("reply_bn") or "",
            "result": {
                "ok": ok,
                "error": result.get("error"),
                "detail": result.get("detail"),
            },
        }
        try:
            saved = engine.remember_turn(
                user_text or f"[{action}]", compact, outcome="ok" if ok else "failed"
            )
            return {"ok": True, "episode_id": saved.get("episode_id")}
        except Exception as exc:
            log.warning("Could not record the action: %s", exc)
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def execute(
        self,
        decision: Dict[str, Any],
        user_text: str = "",
        record: Optional[bool] = None,
        speak: Optional[bool] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Run one decision and return the dispatch report."""
        decision = decision if isinstance(decision, dict) else {}
        action = str(decision.get("action") or "").strip()
        raw_params = params if params is not None else decision.get("params")
        call_params = raw_params if isinstance(raw_params, dict) else {}

        if action not in SUPPORTED_ACTIONS:
            reply = ensure_boss_address(UNKNOWN_ACTION_BN)
            payload = {
                "ok": False,
                "action": action or None,
                "params": call_params,
                "reply_bn": reply,
                "spoken": False,
                "recorded": False,
                "result": None,
                "error": f"unsupported action: {action or '<missing>'}",
            }
            if speak if speak is not None else self.speak_reply:
                payload["spoken"] = self._say(reply)["spoken"]
            self._finish(payload)
            return payload

        handler = self.handlers()[action]
        try:
            result = handler(call_params)
        except Exception as exc:  # a module bug must not kill the assistant
            log.exception("Action %s crashed", action)
            result = {"ok": False, "action": action, "error": f"{type(exc).__name__}: {exc}"}
        result = result if isinstance(result, dict) else {"ok": bool(result)}

        ok = bool(result.get("ok"))
        reply = self._reply_text(result, decision, action)
        should_speak = self.speak_reply if speak is None else bool(speak)
        speech = self._say(reply) if should_speak else {"spoken": False, "engine": None}
        should_record = self.record if record is None else bool(record)
        recorded = (
            self._remember(user_text, decision, {**result, "reply_bn": reply}, action, ok)
            if should_record
            else {"ok": False, "skipped": "record=False"}
        )

        payload: Dict[str, Any] = {
            "ok": ok,
            "action": action,
            "params": call_params,
            "reply_bn": reply,
            "spoken": speech.get("spoken", False),
            "tts_engine": speech.get("engine"),
            "recorded": bool(recorded.get("ok")),
            "result": result,
            "error": None if ok else (result.get("error") or f"{action} failed"),
        }
        if recorded.get("episode_id") is not None:
            payload["episode_id"] = recorded["episode_id"]
        self._finish(payload)
        return payload

    def _finish(self, payload: Dict[str, Any]) -> None:
        self.last_result = payload
        self.history.append(
            {
                "action": payload.get("action"),
                "ok": payload.get("ok"),
                "spoken": payload.get("spoken"),
            }
        )
        self.history = self.history[-50:]

    def process(self, user_text: str, **think_kwargs: Any) -> Dict[str, Any]:
        """brain.think() + execute() in one call.

        The brain already remembers the turn, so the dispatcher does not record
        it again unless this call is given ``record=True``.
        """
        if self.brain is None:
            payload = {
                "ok": False,
                "action": None,
                "params": {},
                "reply_bn": ensure_boss_address("বস, আমার মস্তিষ্ক এখন পাওয়া যাচ্ছে না।"),
                "spoken": False,
                "recorded": False,
                "result": None,
                "error": "no brain configured",
            }
            self._finish(payload)
            return payload

        decision = self.brain.think(user_text, **think_kwargs)
        return self.execute(decision, user_text=user_text, record=False)


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    dispatcher = ActionDispatcher()
    text = " ".join(sys.argv[1:]) or "ব্যাটারি কেমন আছে?"
    print(json.dumps(dispatcher.process(text), ensure_ascii=False, indent=2))
