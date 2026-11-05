"""
call_receptionist.py — ORBIT answers the phone so Boss doesn't have to.

Watch loop (``adb shell dumpsys telephony.registry``):

    mCallState=1  ringing   -> wait ``ring_seconds`` (or answer at once when
    mCallState=2  offhook      ``auto_reception`` is on), then pick up with
    mCallState=0  idle         ``adb shell input keyevent 5``

After answering, ORBIT:

1. tries to route audio to the loudspeaker,
2. speaks the Bengali greeting below,
3. records + transcribes the caller (``voice_engine`` STT),
4. answers back through ``brain.think()`` so the caller gets a real response,
5. logs the transcript + Bengali summary into ``memory_engine`` and returns
   ``{ok, caller, transcript, summary_bn}`` — so Boss can later ask
   "কে কল করেছিল?" and get the answer from memory.

    from call_receptionist import CallReceptionist
    rec = CallReceptionist()
    rec.monitor_once()          # one poll; answers if the threshold passed
    rec.watch()                 # blocking loop for main.py (Ctrl+C to stop)

Speaker routing is ROM-dependent: it is attempted best-effort and reported in
``speaker.attempts``; a failure there never blocks the call.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from config import settings as default_settings
from system_control import CommandResult, run_command

log = logging.getLogger("orbit.receptionist")

GREETING_BN = (
    "নমস্কার, আমি অরবিট বলছি। বস এখন একটু ব্যস্ত আছেন, "
    "আপনার কী জরুরি কথা আছে আমাকে বলুন, আমি বসকে জানিয়ে দেব।"
)

ASK_AGAIN_BN = "দুঃখিত, ঠিক শুনতে পাইনি। আপনার নাম আর জরুরি কথাটা একটু বলবেন?"
NO_BRAIN_REPLY_BN = "বস এখন ব্যস্ত আছেন। আপনার নাম আর জরুরি কথাটা বললে আমি বসকে জানিয়ে দেব।"
ANSWER_FAILED_BN = "কলটা ধরা যায়নি, বস। ফোনটা একবার দেখুন।"
UNKNOWN_CALLER_BN = "অজানা নম্বর"

CALL_STATES = {"0": "idle", "1": "ringing", "2": "offhook", "3": "dialing"}

DEFAULT_ADB = ("adb", "shell")
DUMPSYS_TIMEOUT = 15.0
ANSWER_TIMEOUT = 15.0


class CallReceptionist:
    """Watches for ringing calls, answers them and takes a message."""

    def __init__(
        self,
        brain: Any = None,
        voice: Any = None,
        memory: Any = None,
        control: Any = None,
        runner: Any = run_command,
        settings: Optional[Any] = None,
        adb_prefix: Sequence[str] = DEFAULT_ADB,
        ring_seconds: float = 8.0,
        listen_seconds: float = 10.0,
        auto_reception: bool = False,
        poll_interval: float = 2.0,
        greeting: str = GREETING_BN,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.settings = settings or default_settings
        self.brain = brain
        self.voice = voice
        self.memory = memory
        self.control = control  # optional: maps caller numbers to contact names
        self._runner = runner
        self.adb_prefix = tuple(adb_prefix)
        self.ring_seconds = max(0.0, float(ring_seconds))
        self.listen_seconds = max(1.0, float(listen_seconds))
        self.auto_reception = bool(auto_reception)
        self.poll_interval = max(0.5, float(poll_interval))
        self.greeting = greeting
        self._clock = clock or time.monotonic

        self._ringing_since: Optional[float] = None
        self._ringing_caller: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_call: Optional[Dict[str, Any]] = None

    # -- plumbing ---------------------------------------------------------- #

    def _run(self, cmd: Sequence[str], timeout: Optional[float] = None) -> CommandResult:
        try:
            return self._runner(list(cmd), timeout or DUMPSYS_TIMEOUT)
        except TypeError:  # a fake runner that only takes the command
            return self._runner(list(cmd))

    def _fail(self, error: str, **extra: Any) -> Dict[str, Any]:
        self.last_error = error
        log.warning("Receptionist: %s", error)
        return {"ok": False, "error": error, **extra}

    def _adb(self, *args: str) -> List[str]:
        return [*self.adb_prefix, *args]

    # ------------------------------------------------------------------ #
    # Call state
    # ------------------------------------------------------------------ #

    @staticmethod
    def parse_call_state(text: str) -> Dict[str, Any]:
        """Read ``mCallState`` / ``mCallIncomingNumber`` out of dumpsys output."""
        raw = text or ""
        state_match = re.search(r"mCallState\s*=\s*(\d+)", raw)
        if not state_match:
            return {
                "ok": False,
                "state": "unknown",
                "caller": None,
                "error": "no mCallState in dumpsys telephony.registry output",
            }

        code = state_match.group(1)
        state = CALL_STATES.get(code, f"unknown({code})")

        caller: Optional[str] = None
        number_match = re.search(r"mCallIncomingNumber\s*=\s*([^\n,}\]]+)", raw)
        if number_match:
            candidate = number_match.group(1).strip().strip('"')
            if candidate and candidate.lower() not in ("null", "none", "unknown", "empty"):
                digits = re.sub(r"[^0-9+]", "", candidate)
                if digits and digits not in ("+",):
                    caller = digits

        return {"ok": True, "state": state, "caller": caller, "code": code}

    def current_call(self) -> Dict[str, Any]:
        """One ``dumpsys telephony.registry`` snapshot."""
        result = self._run(self._adb("dumpsys", "telephony.registry"), timeout=DUMPSYS_TIMEOUT)
        if not result.ok:
            self.last_error = result.describe()
            log.warning("dumpsys failed: %s", self.last_error)
            return {"ok": False, "state": "unknown", "caller": None, "error": self.last_error}
        parsed = self.parse_call_state(result.output)
        if not parsed.get("ok"):
            self.last_error = parsed.get("error")
        return parsed

    # ------------------------------------------------------------------ #
    # Answering
    # ------------------------------------------------------------------ #

    def answer(self) -> Dict[str, Any]:
        """Pick up the call (KEYCODE_CALL) and try to enable the speaker."""
        result = self._run(self._adb("input", "keyevent", "5"), timeout=ANSWER_TIMEOUT)
        if not result.ok:
            return self._fail(result.describe(), step="answer", speaker={"ok": False, "attempts": []})

        speaker = self.enable_speaker()
        log.info("Call answered (speaker=%s)", speaker.get("ok"))
        return {
            "ok": True,
            "step": "answer",
            "detail": "adb shell input keyevent 5",
            "speaker": speaker,
        }

    def enable_speaker(self) -> Dict[str, Any]:
        """Best-effort loudspeaker routing (ROM dependent, never fatal).

        Two widely used approaches are tried in order: the HEADSET_PLUG broadcast
        (routes call audio to the "headset"/speaker path) and HEADSETHOOK (toggles
        routing on many devices). The first accepted command wins.
        """
        attempts: List[Dict[str, Any]] = []
        commands = (
            self._adb(
                "am", "broadcast", "-a", "android.intent.action.HEADSET_PLUG",
                "--ei", "state", "1", "--ei", "microphone", "1", "--es", "name", "orbit",
            ),
            self._adb("input", "keyevent", "79"),
        )
        for cmd in commands:
            result = self._run(cmd, timeout=ANSWER_TIMEOUT)
            attempts.append({"cmd": " ".join(cmd), "ok": result.ok, "error": None if result.ok else result.describe()})
            if result.ok:
                return {"ok": True, "attempts": attempts, "detail": " ".join(cmd)}
        log.warning("Could not enable the speakerphone on this ROM")
        return {"ok": False, "attempts": attempts, "error": "speaker routing unavailable"}

    # ------------------------------------------------------------------ #
    # Talking to the caller
    # ------------------------------------------------------------------ #

    def _speak(self, text: str) -> bool:
        if self.voice is None:
            return False
        try:
            return bool(self.voice.speak(text))
        except Exception as exc:
            log.warning("TTS failed: %s", exc)
            return False

    def _listen(self) -> str:
        if self.voice is None:
            return ""
        try:
            heard = self.voice.listen(seconds=self.listen_seconds)
        except TypeError:  # a voice stub with a simpler signature
            try:
                heard = self.voice.listen()
            except Exception as exc:
                log.warning("STT failed: %s", exc)
                return ""
        except Exception as exc:
            log.warning("STT failed: %s", exc)
            return ""
        return str(heard or "").strip()

    def _reply_to(self, heard: str) -> str:
        """A receptionist reply for the caller, via brain when available."""
        if not heard:
            return ASK_AGAIN_BN
        if self.brain is None:
            return NO_BRAIN_REPLY_BN
        try:
            decision = self.brain.think(heard)
            reply = str(decision.get("reply_bn") or "").strip()
            return reply or NO_BRAIN_REPLY_BN
        except Exception as exc:
            log.warning("Brain failed during call: %s", exc)
            return NO_BRAIN_REPLY_BN

    def _caller_name(self, caller: Optional[str]) -> Optional[str]:
        """Use the address book when the number is known."""
        if not caller or self.control is None:
            return None
        try:
            found = self.control.find_contact(caller)
        except Exception as exc:
            log.debug("Contact lookup failed: %s", exc)
            return None
        if found.get("ok") and found.get("name"):
            return str(found["name"])
        return None

    @staticmethod
    def _summarise(who: str, heard: str) -> str:
        if heard:
            return f"{who} থেকে কল এসেছিল। কলার বলেছেন: {heard[:240]}"
        return f"{who} থেকে কল এসেছিল, কিন্তু কলারের কথা স্পষ্ট বোঝা যায়নি।"

    def _log_call(
        self,
        caller: Optional[str],
        name: Optional[str],
        transcript: List[Dict[str, str]],
        summary_bn: str,
    ) -> None:
        if self.memory is None:
            return
        text = " / ".join(f"{turn['role']}: {turn['text']}" for turn in transcript)
        decision = {
            "action": "call_reception",
            "params": {"caller": caller, "name": name, "turns": len(transcript)},
            "reply_bn": summary_bn,
        }
        try:
            self.memory.remember_turn(text or summary_bn, decision, outcome="incoming_call")
        except Exception as exc:
            log.warning("Could not log the call to memory: %s", exc)

    # ------------------------------------------------------------------ #
    # The full reception
    # ------------------------------------------------------------------ #

    def receive(self, caller: Optional[str] = None) -> Dict[str, Any]:
        """Answer, greet, take the message, reply and log it."""
        answered = self.answer()
        name = self._caller_name(caller)
        who = name or caller or UNKNOWN_CALLER_BN

        if not answered.get("ok"):
            summary = ANSWER_FAILED_BN
            self._speak(summary)
            result = {
                "ok": False,
                "action": "call_reception",
                "answered": False,
                "caller": caller,
                "caller_name": name,
                "transcript": [],
                "transcript_text": "",
                "summary_bn": summary,
                "error": answered.get("error"),
                "speaker": answered.get("speaker", {}),
            }
            self.last_call = result
            return result

        self._speak(self.greeting)
        transcript: List[Dict[str, str]] = [{"role": "orbit", "text": self.greeting}]

        heard = self._listen()
        if heard:
            transcript.append({"role": "caller", "text": heard})

        reply = self._reply_to(heard)
        self._speak(reply)
        transcript.append({"role": "orbit", "text": reply})

        summary = self._summarise(who, heard)
        self._log_call(caller, name, transcript, summary)

        result = {
            "ok": True,
            "action": "call_reception",
            "answered": True,
            "caller": caller,
            "caller_name": name,
            "transcript": transcript,
            "transcript_text": "\n".join(
                f"{'ORBIT' if turn['role'] == 'orbit' else 'CALLER'}: {turn['text']}" for turn in transcript
            ),
            "heard": heard,
            "summary_bn": summary,
            "greeting_bn": self.greeting,
            "speaker": answered.get("speaker", {}),
        }
        self.last_call = result
        log.info("Reception finished for %s (%d turns)", who, len(transcript))
        return result

    # ------------------------------------------------------------------ #
    # Watch loop
    # ------------------------------------------------------------------ #

    def monitor_once(self, auto: Optional[bool] = None) -> Dict[str, Any]:
        """Poll once; answer when the ring threshold (or auto mode) is met."""
        call = self.current_call()
        if not call.get("ok"):
            return {
                "ok": False,
                "state": "unknown",
                "caller": None,
                "answered": False,
                "error": call.get("error"),
            }

        state = call.get("state")
        caller = call.get("caller")

        if state != "ringing":
            self._ringing_since = None
            self._ringing_caller = None
            return {
                "ok": True,
                "state": state,
                "caller": caller,
                "answered": False,
                "ring_seconds": 0.0,
            }

        now = self._clock()
        if self._ringing_since is None or self._ringing_caller != caller:
            self._ringing_since = now
            self._ringing_caller = caller

        elapsed = max(0.0, now - self._ringing_since)
        should_answer = (self.auto_reception if auto is None else bool(auto)) or elapsed >= self.ring_seconds

        if not should_answer:
            return {
                "ok": True,
                "state": "ringing",
                "caller": caller,
                "answered": False,
                "ring_seconds": elapsed,
                "threshold": self.ring_seconds,
            }

        result = self.receive(caller=caller)
        result.update({"state": "answered" if result.get("ok") else "ringing", "ring_seconds": elapsed})
        return result

    def watch(
        self,
        max_seconds: Optional[float] = None,
        stop_event: Any = None,
    ) -> Dict[str, Any]:
        """Blocking poll loop for the always-on service."""
        started = self._clock()
        polls = 0
        while True:
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                break
            try:
                self.monitor_once()
                polls += 1
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("Reception poll failed: %s", exc)
            if max_seconds is not None and (self._clock() - started) >= max_seconds:
                break
            time.sleep(self.poll_interval)
        return {"ok": True, "polls": polls, "last_call": self.last_call}


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import json

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    receptionist = CallReceptionist()
    print("adb:", receptionist.current_call())
    print("Ctrl+C to stop")
    try:
        while True:
            output = receptionist.monitor_once()
            print(json.dumps(output, ensure_ascii=False)[:300])
            time.sleep(receptionist.poll_interval)
    except KeyboardInterrupt:
        print("stopped")
