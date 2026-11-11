"""
tests/test_actions.py — the dispatcher routes all 13 actions, speaks the real
outcome and records it. Every module is a fake; nothing touches the phone.
"""

from __future__ import annotations

import unittest

from actions import ActionDispatcher
from brain import SUPPORTED_ACTIONS


class FakeControl:
    def __init__(self):
        self.calls = []
        self.raise_on = set()

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if name in self.raise_on:
            raise RuntimeError(f"{name} exploded")

    def torch(self, state="on"):
        self._record("torch", state)
        return {"ok": True, "state": state, "detail": f"termux-torch {state}"}

    def battery_status(self):
        self._record("battery")
        return {"ok": True, "percentage": 87, "status": "DISCHARGING",
                "message_bn": "ব্যাটারি ৮৭ শতাংশ, চার্জ কমছে।"}

    def set_volume(self, level=None, stream="music", direction=None):
        self._record("volume", level, stream, direction)
        return {"ok": True, "level": level, "stream": stream,
                "message_bn": "music ভলিউম ৪০ শতাংশ করা হয়েছে।"}

    def toggle_wifi(self, state="on"):
        self._record("wifi", state)
        return {"ok": True, "state": state}


class FakePc:
    def __init__(self):
        self.calls = []

    def open_app(self, name):
        self.calls.append(("open_app", name))
        return {"ok": True, "app": name, "message_bn": f"বস, {name} খুলছি।"}

    def open_website(self, url):
        self.calls.append(("open_website", url))
        return {"ok": True, "url": url, "message_bn": f"বস, {url} খুলছি।"}

    def open_folder(self, path):
        self.calls.append(("open_folder", path))
        return {"ok": True, "path": path, "message_bn": "বস, ফোল্ডার খুলছি।"}

    def system_health(self):
        self.calls.append(("system_health",))
        return {
            "ok": True,
            "cpu_percent": 12.0,
            "ram_percent": 40.0,
            "summary_bn": "বস, সিপিইউ ১২ শতাংশ, সব ঠিক আছে।",
        }


class FakeComm:
    def __init__(self, ok=True, message_bn=None):
        self.ok = ok
        self.message_bn = message_bn
        self.calls = []

    def make_call(self, contact=None, number=None):
        self.calls.append(("call", contact, number))
        if not self.ok:
            return {"ok": False, "error": "no SIM", "message_bn": self.message_bn}
        return {"ok": True, "name": contact, "number": number, "message_bn": f"{contact}কে কল দিচ্ছি।"}

    def send_sms(self, contact=None, number=None, message=""):
        self.calls.append(("sms", contact, number, message))
        if not self.ok:
            return {"ok": False, "error": "no radio", "message_bn": self.message_bn}
        return {"ok": True, "name": contact, "message": message, "message_bn": "মেসেজ পাঠাচ্ছি।"}


class FakeAdb:
    def __init__(self):
        self.calls = []

    def send_whatsapp(self, contact=None, number=None, message="", press_send=False):
        self.calls.append(("whatsapp", contact, number, message, press_send))
        return {"ok": True, "message_bn": "হোয়াটসঅ্যাপে মেসেজ লিখে রেখেছি।"}

    def open_app(self, app_name):
        self.calls.append(("open_app", app_name))
        return {"ok": True, "name_bn": "ইউটিউব", "message_bn": "ইউটিউব খুলছি।"}

    def read_screen(self, prompt_bn=""):
        self.calls.append(("read_screen", prompt_bn))
        return {"ok": True, "description_bn": "স্ক্রিনে হোয়াটসঅ্যাপ খোলা।", "reply_bn": "স্ক্রিনে হোয়াটসঅ্যাপ খোলা।"}


class FakeBriefing:
    def __init__(self):
        self.calls = []

    def market_news(self, topic="general"):
        self.calls.append(topic)
        return {"ok": True, "reply_bn": "বস, আজ নিফটি বেড়েছে।", "summary_bn": "বস, আজ নিফটি বেড়েছে।"}


class FakeSecurity:
    def __init__(self):
        self.calls = []

    def intruder_check(self, capture=True):
        self.calls.append(capture)
        return {"ok": True, "summary_bn": "ছবি তোলা হয়েছে, লোকেশন পাওয়া গেছে।", "photo_path": "/tmp/x.jpg"}


class FakeReceptionist:
    def __init__(self):
        self.calls = 0

    def answer(self):
        self.calls += 1
        return {"ok": True, "step": "answer", "detail": "adb shell input keyevent 5"}


class FakeVoice:
    def __init__(self, ok=True, raises=False):
        self.ok = ok
        self.raises = raises
        self.spoken = []
        self.last_tts_engine = "edge-tts"

    def speak(self, text, **_kwargs):
        if self.raises:
            raise RuntimeError("no TTS")
        self.spoken.append(text)
        return self.ok


class FakeMemory:
    def __init__(self, raises=False):
        self.turns = []
        self.raises = raises

    def remember_turn(self, text, decision, outcome="ok"):
        if self.raises:
            raise RuntimeError("disk full")
        self.turns.append({"text": text, "decision": decision, "outcome": outcome})
        return {"ok": True, "episode_id": len(self.turns)}


class FakeBrain:
    def __init__(self, decision=None):
        self.decision = decision or {"action": "torch", "params": {"state": "on"}, "reply_bn": "বস, টর্চ জ্বালাচ্ছি।"}
        self.seen = []

    def think(self, text, *args, **kwargs):
        self.seen.append(text)
        return self.decision


def make(**kwargs):
    control = kwargs.pop("control", FakeControl())
    comm = kwargs.pop("comm", FakeComm())
    adb = kwargs.pop("adb", FakeAdb())
    pc = kwargs.pop("pc", FakePc())
    briefing = kwargs.pop("briefing", FakeBriefing())
    security = kwargs.pop("security", FakeSecurity())
    receptionist = kwargs.pop("receptionist", FakeReceptionist())
    voice = kwargs.pop("voice", FakeVoice())
    memory = kwargs.pop("memory", FakeMemory())
    return ActionDispatcher(
        control=control,
        comm=comm,
        adb=adb,
        pc=pc,
        briefing=briefing,
        security=security,
        receptionist=receptionist,
        voice=voice,
        memory=memory,
        **kwargs,
    ), {
        "control": control, "comm": comm, "adb": adb, "pc": pc, "briefing": briefing,
        "security": security, "receptionist": receptionist, "voice": voice, "memory": memory,
    }


def decision(action, params=None, reply="বস, করে দিচ্ছি।"):
    return {"action": action, "params": params or {}, "reply_bn": reply}


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #

class TestContract(unittest.TestCase):
    def test_every_whitelisted_action_has_a_handler(self):
        dispatcher, _ = make()
        self.assertEqual(set(dispatcher.handlers()), set(SUPPORTED_ACTIONS))
        self.assertEqual(len(SUPPORTED_ACTIONS), 16)

    def test_success_reply_always_addresses_boss(self):
        dispatcher, _ = make()
        result = dispatcher.execute(decision("torch", {"state": "on"}, reply="টর্চ জ্বালাচ্ছি।"))
        self.assertTrue(result["reply_bn"].startswith("বস"))

    def test_unknown_action_does_nothing(self):
        dispatcher, fakes = make()
        result = dispatcher.execute(decision("launch_missile", {"x": 1}))

        self.assertFalse(result["ok"])
        self.assertIn("চিনি না", result["reply_bn"])
        self.assertEqual(fakes["control"].calls, [])
        self.assertEqual(fakes["memory"].turns, [])
        self.assertFalse(result["recorded"])

    def test_missing_action(self):
        dispatcher, _ = make()
        result = dispatcher.execute({})
        self.assertFalse(result["ok"])
        self.assertIsNone(result["action"])

    def test_history_and_last_result(self):
        dispatcher, _ = make()
        dispatcher.execute(decision("torch", {"state": "off"}), user_text="টর্চ নেভাও")
        self.assertEqual(dispatcher.last_result["action"], "torch")
        self.assertEqual(dispatcher.history[-1]["action"], "torch")
        self.assertTrue(dispatcher.history[-1]["ok"])


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #

class TestRouting(unittest.TestCase):
    def test_call(self):
        dispatcher, fakes = make()
        result = dispatcher.execute(decision("call", {"contact": "রহিম"}))
        self.assertTrue(result["ok"])
        self.assertEqual(fakes["comm"].calls[0], ("call", "রহিম", None))
        self.assertAccessibleReply(result)

    def test_answer_call(self):
        dispatcher, fakes = make()
        result = dispatcher.execute(decision("answer_call"))
        self.assertTrue(result["ok"])
        self.assertEqual(fakes["receptionist"].calls, 1)
        self.assertAccessibleReply(result)

    def test_sms(self):
        dispatcher, fakes = make()
        result = dispatcher.execute(decision("sms", {"contact": "রহিম", "message": "আসছি"}))
        self.assertTrue(result["ok"])
        self.assertEqual(fakes["comm"].calls[0], ("sms", "রহিম", None, "আসছি"))

    def test_whatsapp(self):
        dispatcher, fakes = make()
        result = dispatcher.execute(decision("whatsapp", {"contact": "রহিম", "message": "hi"}))
        self.assertTrue(result["ok"])
        self.assertEqual(fakes["adb"].calls[0], ("whatsapp", "রহিম", None, "hi", False))

    def test_whatsapp_auto_send_flag(self):
        dispatcher, fakes = make(auto_send_whatsapp=True)
        dispatcher.execute(decision("whatsapp", {"contact": "রহিম", "message": "hi"}))
        self.assertTrue(fakes["adb"].calls[0][4])

    def test_torch(self):
        dispatcher, fakes = make()
        dispatcher.execute(decision("torch", {"state": "off"}))
        self.assertEqual(fakes["control"].calls, [("torch", ("off",), {})])

    def test_battery_uses_module_message(self):
        dispatcher, fakes = make()
        result = dispatcher.execute(decision("battery", reply="ব্যাটারি দেখছি।"))
        self.assertTrue(result["ok"])
        self.assertIn("৮৭", result["reply_bn"])
        self.assertEqual(fakes["control"].calls[0][0], "battery")

    def test_volume_defaults(self):
        dispatcher, fakes = make()
        dispatcher.execute(decision("volume", {"level": 40}))
        name, args, kwargs = fakes["control"].calls[0]
        self.assertEqual(name, "volume")
        self.assertEqual(args[0], 40)
        self.assertEqual(args[1], "music")

    def test_volume_direction(self):
        dispatcher, fakes = make()
        dispatcher.execute(decision("volume", {"direction": "up"}))
        self.assertEqual(fakes["control"].calls[0][1][2], "up")

    def test_wifi(self):
        dispatcher, fakes = make()
        dispatcher.execute(decision("wifi", {"state": "off"}))
        self.assertEqual(fakes["control"].calls[0], ("wifi", ("off",), {}))

    def test_intruder_check(self):
        dispatcher, fakes = make()
        result = dispatcher.execute(decision("intruder_check", {"capture": False}))
        self.assertTrue(result["ok"])
        self.assertEqual(fakes["security"].calls, [False])
        self.assertIn("লোকেশন", result["reply_bn"])

    def test_open_app(self):
        dispatcher, fakes = make()
        result = dispatcher.execute(decision("open_app", {"app": "ইউটিউব"}))
        self.assertTrue(result["ok"])
        self.assertEqual(fakes["pc"].calls[0], ("open_app", "ইউটিউব"))
        self.assertIn("ইউটিউব", result["reply_bn"])

    def test_open_website(self):
        dispatcher, fakes = make()
        result = dispatcher.execute(decision("open_website", {"url": "github.com"}))
        self.assertTrue(result["ok"])
        self.assertEqual(fakes["pc"].calls[0], ("open_website", "github.com"))
        self.assertIn("github.com", result["reply_bn"])

    def test_open_folder(self):
        dispatcher, fakes = make()
        result = dispatcher.execute(decision("open_folder", {"path": "~/Downloads"}))
        self.assertTrue(result["ok"])
        self.assertEqual(fakes["pc"].calls[0], ("open_folder", "~/Downloads"))

    def test_pc_health(self):
        dispatcher, fakes = make()
        result = dispatcher.execute(decision("pc_health"))
        self.assertTrue(result["ok"])
        self.assertEqual(fakes["pc"].calls[0], ("system_health",))
        self.assertIn("সিপিইউ", result["reply_bn"])

    def test_read_screen_prompt(self):
        dispatcher, fakes = make()
        result = dispatcher.execute(decision("read_screen"))
        self.assertTrue(result["ok"])
        self.assertTrue(fakes["adb"].calls[0][1])
        self.assertIn("হোয়াটসঅ্যাপ", result["reply_bn"])

    def test_market_news(self):
        dispatcher, fakes = make()
        result = dispatcher.execute(decision("market_news", {"topic": "stocks"}))
        self.assertTrue(result["ok"])
        self.assertEqual(fakes["briefing"].calls, ["stocks"])
        self.assertIn("নিফটি", result["reply_bn"])

    def test_speak_action_says_the_text(self):
        dispatcher, fakes = make()
        result = dispatcher.execute(decision("speak", {"text": "আজ বিকেল পাঁচটায় মিটিং।"}))
        self.assertTrue(result["ok"])
        self.assertIn("মিটিং", fakes["voice"].spoken[-1])
        self.assertTrue(result["spoken"])

    def test_speak_action_empty_text(self):
        dispatcher, fakes = make()
        result = dispatcher.execute(decision("speak", {}))
        self.assertFalse(result["ok"])
        self.assertIn("কী বলব", result["reply_bn"])

    def assertAccessibleReply(self, result):
        self.assertTrue(result["reply_bn"].startswith("বস"), result["reply_bn"])


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #

class TestFailures(unittest.TestCase):
    def test_module_failure_message_is_spoken_not_the_optimistic_line(self):
        comm = FakeComm(ok=False, message_bn="রহিমকে কল দেওয়া গেল না।")
        dispatcher, fakes = make(comm=comm)
        result = dispatcher.execute(decision("call", {"contact": "রহিম"}, reply="বস, কল দিচ্ছি।"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["reply_bn"], "বস, রহিমকে কল দেওয়া গেল না।")
        self.assertEqual(fakes["voice"].spoken[-1], result["reply_bn"])
        self.assertNotIn("দিচ্ছি", result["reply_bn"])

    def test_failure_without_message_gets_generic_bengali(self):
        comm = FakeComm(ok=False)
        dispatcher, _ = make(comm=comm)
        result = dispatcher.execute(decision("call", {"contact": "রহিম"}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reply_bn"], "বস, কলটা করতে পারলাম না।")

    def test_handler_crash_is_contained(self):
        control = FakeControl()
        control.raise_on.add("torch")
        dispatcher, fakes = make(control=control)
        result = dispatcher.execute(decision("torch", {"state": "on"}))

        self.assertFalse(result["ok"])
        self.assertIn("RuntimeError", result["error"])
        self.assertTrue(result["reply_bn"].startswith("বস"))
        self.assertEqual(fakes["memory"].turns[-1]["outcome"], "failed")

    def test_voice_failure_does_not_fail_the_action(self):
        dispatcher, _ = make(voice=FakeVoice(raises=True))
        result = dispatcher.execute(decision("torch", {"state": "on"}))
        self.assertTrue(result["ok"])
        self.assertFalse(result["spoken"])

    def test_memory_failure_is_swallowed(self):
        dispatcher, _ = make(memory=FakeMemory(raises=True))
        result = dispatcher.execute(decision("torch", {"state": "on"}))
        self.assertTrue(result["ok"])
        self.assertFalse(result["recorded"])
        self.assertIsNone(result["error"])


# --------------------------------------------------------------------------- #
# Speak + record options
# --------------------------------------------------------------------------- #

class TestOptions(unittest.TestCase):
    def test_speak_reply_false(self):
        dispatcher, fakes = make(speak_reply=False)
        result = dispatcher.execute(decision("torch", {"state": "on"}))
        self.assertFalse(result["spoken"])
        self.assertEqual(fakes["voice"].spoken, [])
        self.assertTrue(result["reply_bn"])

    def test_per_call_speak_override(self):
        dispatcher, fakes = make(speak_reply=False)
        result = dispatcher.execute(decision("torch", {"state": "on"}), speak=True)
        self.assertTrue(result["spoken"])
        self.assertEqual(len(fakes["voice"].spoken), 1)

    def test_records_outcome(self):
        dispatcher, fakes = make()
        result = dispatcher.execute(decision("torch", {"state": "on"}), user_text="টর্চ জ্বালাও")

        self.assertTrue(result["recorded"])
        turn = fakes["memory"].turns[0]
        self.assertEqual(turn["outcome"], "ok")
        self.assertEqual(turn["decision"]["action"], "torch")
        self.assertEqual(turn["decision"]["result"]["ok"], True)
        self.assertEqual(turn["text"], "টর্চ জ্বালাও")
        self.assertEqual(result["episode_id"], 1)

    def test_record_false(self):
        dispatcher, fakes = make()
        result = dispatcher.execute(decision("torch", {"state": "on"}), user_text="x", record=False)
        self.assertFalse(result["recorded"])
        self.assertEqual(fakes["memory"].turns, [])

    def test_failed_action_recorded_as_failed(self):
        dispatcher, fakes = make(comm=FakeComm(ok=False))
        dispatcher.execute(decision("call", {"contact": "রহিম"}))
        self.assertEqual(fakes["memory"].turns[-1]["outcome"], "failed")

    def test_process_uses_brain_without_double_recording(self):
        brain = FakeBrain()
        dispatcher, fakes = make(brain=brain)
        result = dispatcher.process("টর্চ জ্বালাও")

        self.assertEqual(brain.seen, ["টর্চ জ্বালাও"])
        self.assertTrue(result["ok"])
        self.assertEqual(fakes["memory"].turns, [])
        self.assertEqual(fakes["control"].calls[0][0], "torch")

    def test_process_without_brain(self):
        dispatcher, _ = make()
        result = dispatcher.process("কিছু একটা")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no brain configured")
        self.assertTrue(result["reply_bn"].startswith("বস"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
