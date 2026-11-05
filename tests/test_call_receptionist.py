"""
tests/test_call_receptionist.py — dumpsys parsing, the ring threshold and the
full reception flow. adb, voice, brain and memory are all fakes.
"""

from __future__ import annotations

import unittest

from call_receptionist import (
    ANSWER_FAILED_BN,
    ASK_AGAIN_BN,
    GREETING_BN,
    NO_BRAIN_REPLY_BN,
    CallReceptionist,
)
from system_control import CommandResult

RINGING = """
  mCallState=1
  mCallIncomingNumber=+8801712345678
  mForegroundCallState=ACTIVE
"""
IDLE = """
  mCallState=0
  mCallIncomingNumber=null
"""
OFFHOOK = """
  mCallState=2
  mCallIncomingNumber=+8801712345678
"""


class AdbRunner:
    """Answers dumpsys; every other adb command succeeds (or fails on request)."""

    def __init__(self, dumpsys=IDLE, adb_error=None, fail_contains=None):
        self.dumpsys = dumpsys
        self.adb_error = adb_error
        self.fail_contains = fail_contains
        self.calls = []

    def __call__(self, cmd, timeout=None):
        self.calls.append(list(cmd))
        joined = " ".join(str(part) for part in cmd)
        if self.adb_error:
            return CommandResult(code=127, error=self.adb_error)
        if "dumpsys" in cmd:
            return CommandResult(code=0, stdout=self.dumpsys)
        if self.fail_contains and self.fail_contains in joined:
            return CommandResult(code=1, stderr=f"failed: {self.fail_contains}")
        return CommandResult(code=0)

    def count(self, *needles):
        return len([c for c in self.calls if all(n in " ".join(c) for n in needles)])


class FakeVoice:
    def __init__(self, transcript="আমি করিম বলছি, জরুরি একটা কাজ আছে", speak_ok=True):
        self.transcript = transcript
        self.speak_ok = speak_ok
        self.spoken = []
        self.listened = 0
        self.speak_raises = False
        self.listen_raises = False

    def speak(self, text):
        if self.speak_raises:
            raise RuntimeError("no TTS")
        self.spoken.append(text)
        return self.speak_ok

    def listen(self, seconds=6):
        if self.listen_raises:
            raise RuntimeError("no mic")
        self.listened += 1
        return self.transcript


class FakeBrain:
    def __init__(self, reply="বস, আমি কথাটা নোট করে রেখেছি।"):
        self.reply = reply
        self.seen = []
        self.raises = False

    def think(self, text, *args, **kwargs):
        self.seen.append(text)
        if self.raises:
            raise RuntimeError("brain offline")
        return {"action": "speak", "params": {"text": self.reply}, "reply_bn": self.reply}


class FakeMemory:
    def __init__(self):
        self.turns = []
        self.raises = False

    def remember_turn(self, text, decision, outcome="ok"):
        if self.raises:
            raise RuntimeError("disk full")
        self.turns.append({"text": text, "decision": decision, "outcome": outcome})
        return {"ok": True, "episode_id": len(self.turns)}


class FakeControl:
    """Address-book stub: one known caller."""

    def __init__(self, name="রহিম"):
        self.name = name
        self.queries = []

    def find_contact(self, query):
        self.queries.append(query)
        return {"ok": True, "name": self.name, "number": query}


def make(
    dumpsys=IDLE,
    clock=(1000.0,),
    ring_seconds=8.0,
    transcript="আমি করিম বলছি, জরুরি একটা কাজ আছে",
    **kwargs
):
    runner = kwargs.pop("runner", None) or AdbRunner(dumpsys)
    voice = kwargs.pop("voice", None) or FakeVoice(transcript)
    brain = kwargs.pop("brain", None) or FakeBrain()
    memory = kwargs.pop("memory", None) or FakeMemory()
    rec = CallReceptionist(
        brain=brain,
        voice=voice,
        memory=memory,
        runner=runner,
        ring_seconds=ring_seconds,
        clock=lambda: clock[0],
        **kwargs,
    )
    return rec


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

class TestParse(unittest.TestCase):
    def test_ringing_with_number(self):
        parsed = CallReceptionist.parse_call_state(RINGING)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["state"], "ringing")
        self.assertEqual(parsed["caller"], "+8801712345678")

    def test_idle_null_number(self):
        parsed = CallReceptionist.parse_call_state(IDLE)
        self.assertEqual(parsed["state"], "idle")
        self.assertIsNone(parsed["caller"])

    def test_offhook(self):
        self.assertEqual(CallReceptionist.parse_call_state(OFFHOOK)["state"], "offhook")

    def test_spaced_and_quoted_number(self):
        parsed = CallReceptionist.parse_call_state('mCallState=1\nmCallIncomingNumber="01712 345678"')
        self.assertEqual(parsed["state"], "ringing")
        self.assertEqual(parsed["caller"], "01712345678")

    def test_missing_state(self):
        parsed = CallReceptionist.parse_call_state("nothing useful here")
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["state"], "unknown")
        self.assertIn("mCallState", parsed["error"])

    def test_current_call_uses_adb(self):
        runner = AdbRunner(RINGING)
        rec = make(runner=runner)
        call = rec.current_call()
        self.assertTrue(call["ok"])
        self.assertEqual(call["caller"], "+8801712345678")
        self.assertEqual(runner.calls[0], ["adb", "shell", "dumpsys", "telephony.registry"])

    def test_adb_missing(self):
        rec = make(runner=AdbRunner(adb_error="adb: not found"))
        call = rec.current_call()
        self.assertFalse(call["ok"])
        self.assertIn("adb", call["error"])


# --------------------------------------------------------------------------- #
# Answering + speaker
# --------------------------------------------------------------------------- #

class TestAnswer(unittest.TestCase):
    def test_answer_presses_keyevent_and_speaker(self):
        runner = AdbRunner()
        rec = make(runner=runner)
        result = rec.answer()

        self.assertTrue(result["ok"])
        self.assertEqual(runner.count("keyevent", "5"), 1)
        self.assertTrue(result["speaker"]["attempts"])
        self.assertIn("HEADSET_PLUG", result["speaker"]["attempts"][0]["cmd"])

    def test_speaker_falls_back_to_second_strategy(self):
        runner = AdbRunner(fail_contains="HEADSET_PLUG")
        rec = make(runner=runner)
        speaker = rec.enable_speaker()

        self.assertTrue(speaker["ok"])
        self.assertEqual(len(speaker["attempts"]), 2)
        self.assertFalse(speaker["attempts"][0]["ok"])
        self.assertIn("79", speaker["attempts"][1]["cmd"])

    def test_answer_failure(self):
        runner = AdbRunner(adb_error="adb: device offline")
        rec = make(runner=runner)
        result = rec.answer()
        self.assertFalse(result["ok"])
        self.assertEqual(result["step"], "answer")
        self.assertIn("offline", result["error"])


# --------------------------------------------------------------------------- #
# Receiving a call
# --------------------------------------------------------------------------- #

class TestReceive(unittest.TestCase):
    def test_full_reception(self):
        voice = FakeVoice()
        brain = FakeBrain()
        memory = FakeMemory()
        control = FakeControl()
        rec = make(voice=voice, brain=brain, memory=memory, control=control)

        result = rec.receive(caller="+8801712345678")

        self.assertTrue(result["ok"])
        self.assertTrue(result["answered"])
        self.assertEqual(result["caller"], "+8801712345678")
        self.assertEqual(result["caller_name"], "রহিম")

        # greeting first, then the brain's answer
        self.assertEqual(voice.spoken[0], GREETING_BN)
        self.assertIn("বস", GREETING_BN)
        self.assertEqual(voice.spoken[1], brain.reply)
        self.assertEqual(brain.seen, ["আমি করিম বলছি, জরুরি একটা কাজ আছে"])

        self.assertEqual([t["role"] for t in result["transcript"]], ["orbit", "caller", "orbit"])
        self.assertIn("CALLER: আমি করিম", result["transcript_text"])
        self.assertIn("রহিম", result["summary_bn"])
        self.assertIn("করিম", result["summary_bn"])

        self.assertEqual(len(memory.turns), 1)
        logged = memory.turns[0]
        self.assertEqual(logged["outcome"], "incoming_call")
        self.assertEqual(logged["decision"]["action"], "call_reception")
        self.assertEqual(logged["decision"]["params"]["caller"], "+8801712345678")
        self.assertIn("করিম", logged["text"])
        self.assertEqual(rec.last_call["summary_bn"], result["summary_bn"])

    def test_empty_transcript_still_answers_and_logs(self):
        voice = FakeVoice(transcript="")
        memory = FakeMemory()
        result = make(voice=voice, memory=memory).receive(caller="01712345678")

        self.assertTrue(result["ok"])
        self.assertEqual(voice.spoken[0], GREETING_BN)
        self.assertEqual(voice.spoken[1], ASK_AGAIN_BN)
        self.assertIn("স্পষ্ট বোঝা যায়নি", result["summary_bn"])
        self.assertEqual(len(memory.turns), 1)

    def test_brain_failure_uses_receptionist_fallback(self):
        brain = FakeBrain()
        brain.raises = True
        result = make(brain=brain).receive(caller="01712345678")
        self.assertTrue(result["ok"])
        self.assertEqual(result["transcript"][-1]["text"], NO_BRAIN_REPLY_BN)

    def test_voice_failures_do_not_break_the_call(self):
        voice = FakeVoice()
        voice.speak_raises = True
        voice.listen_raises = True
        result = make(voice=voice).receive(caller="01712345678")
        self.assertTrue(result["ok"])
        self.assertEqual(result["heard"], "")
        self.assertEqual(len(result["transcript"]), 2)

    def test_memory_failure_does_not_break_the_call(self):
        memory = FakeMemory()
        memory.raises = True
        result = make(memory=memory).receive(caller="01712345678")
        self.assertTrue(result["ok"])
        self.assertEqual(memory.turns, [])

    def test_unknown_caller(self):
        result = make().receive()
        self.assertTrue(result["ok"])
        self.assertIsNone(result["caller"])
        self.assertIn("অজানা নম্বর", result["summary_bn"])

    def test_answer_failure_is_reported_and_spoken(self):
        voice = FakeVoice()
        rec = make(runner=AdbRunner(adb_error="adb: no permission"), voice=voice)

        result = rec.receive(caller="01712345678")

        self.assertFalse(result["ok"])
        self.assertFalse(result["answered"])
        self.assertEqual(result["summary_bn"], ANSWER_FAILED_BN)
        self.assertEqual(result["transcript"], [])
        self.assertEqual(voice.spoken, [ANSWER_FAILED_BN])
        self.assertEqual(voice.listened, 0)
        self.assertIn("permission", result["error"])


# --------------------------------------------------------------------------- #
# Ring threshold
# --------------------------------------------------------------------------- #

class TestMonitor(unittest.TestCase):
    def test_ring_below_threshold_does_not_answer(self):
        clock = [1000.0]
        runner = AdbRunner(RINGING)
        rec = make(runner=runner, clock=clock)

        first = rec.monitor_once()
        self.assertTrue(first["ok"])
        self.assertFalse(first["answered"])
        self.assertEqual(first["state"], "ringing")
        self.assertEqual(first["caller"], "+8801712345678")
        self.assertIn("threshold", first)

        clock[0] += 3.0
        second = rec.monitor_once()
        self.assertFalse(second["answered"])
        self.assertGreaterEqual(second["ring_seconds"], 3.0)

        self.assertEqual(runner.count("keyevent", "5"), 0)

    def test_answers_after_threshold(self):
        clock = [1000.0]
        voice = FakeVoice()
        memory = FakeMemory()
        rec = make(runner=AdbRunner(RINGING), clock=clock, ring_seconds=8.0, voice=voice, memory=memory)

        rec.monitor_once()
        clock[0] += 8.5
        result = rec.monitor_once()

        self.assertTrue(result["answered"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "answered")
        self.assertEqual(voice.spoken[0], GREETING_BN)
        self.assertEqual(len(memory.turns), 1)

    def test_auto_reception_answers_immediately(self):
        rec = make(runner=AdbRunner(RINGING), auto_reception=True)
        result = rec.monitor_once()
        self.assertTrue(result["answered"])
        self.assertEqual(result["state"], "answered")

    def test_auto_override_false_waits(self):
        rec = make(runner=AdbRunner(RINGING), auto_reception=True)
        result = rec.monitor_once(auto=False)
        self.assertFalse(result["answered"])

    def test_idle_resets_the_ring_timer(self):
        clock = [1000.0]
        runner = AdbRunner(RINGING)
        rec = make(runner=runner, clock=clock)

        rec.monitor_once()          # ringing starts at 1000
        clock[0] += 5.0
        runner.dumpsys = IDLE
        idle = rec.monitor_once()   # call cancelled
        self.assertEqual(idle["state"], "idle")
        self.assertFalse(idle["answered"])

        runner.dumpsys = RINGING
        clock[0] += 100.0
        fresh = rec.monitor_once()  # new ring starts now, not 100s ago
        self.assertFalse(fresh["answered"])

        clock[0] += 8.0
        self.assertTrue(rec.monitor_once()["answered"])

    def test_unknown_state_does_not_answer(self):
        rec = make(runner=AdbRunner("garbage"))
        result = rec.monitor_once()
        self.assertFalse(result["ok"])
        self.assertFalse(result["answered"])

    def test_new_caller_resets_the_timer(self):
        clock = [1000.0]
        runner = AdbRunner(RINGING)
        rec = make(runner=runner, clock=clock)
        rec.monitor_once()

        runner.dumpsys = RINGING.replace("+8801712345678", "+8801999999999")
        clock[0] += 20.0
        result = rec.monitor_once()
        self.assertFalse(result["answered"])
        self.assertEqual(result["caller"], "+8801999999999")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
