"""
tests/test_notification_responder.py — notification parsing, dedupe, memory
logging and the reply channels. No Termux binary, no phone, no brain network.
"""

from __future__ import annotations

import json
import unittest

from notification_responder import NotificationResponder
from system_control import CommandResult

WHATSAPP_MSG = {
    "key": "wa:1",
    "id": "1",
    "packageName": "com.whatsapp",
    "appName": "WhatsApp",
    "title": "রহিম",
    "content": "কাল দেখা হবে?",
    "when": 1700000000000,
}
TELEGRAM_MSG = {
    "key": "tg:1",
    "packageName": "org.telegram.messenger",
    "title": "Karim",
    "content": "sent the file",
    "when": 1700000001000,
}
SMS_MSG = {
    "key": "sms:1",
    "packageName": "com.google.android.apps.messaging",
    "title": "01712345678",
    "content": "OTP 445566",
    "when": 1700000002000,
}
SMS_CLEAN = {
    "key": "sms:2",
    "packageName": "com.google.android.apps.messaging",
    "title": "রহিম",
    "content": "কাল আসছি",
    "when": 1700000002500,
}
BANK_ALERT = {
    "key": "bank:1",
    "packageName": "com.whatsapp",
    "title": "SBI",
    "content": "Rs.500 debited from your a/c 1234",
    "when": 1700000004000,
}
CHROME_DOWNLOAD = {
    "key": "chrome:1",
    "packageName": "com.android.chrome",
    "title": "Download complete",
    "content": "report.pdf",
    "when": 1700000003000,
}


class NotifRunner:
    """Answers termux-notification-list, and fakes the uiautomator dump."""

    def __init__(self, notifications=None, list_error=None, ui_xml=None, pull_ok=True):
        self.notifications = notifications if notifications is not None else []
        self.list_error = list_error
        self.ui_xml = ui_xml
        self.pull_ok = pull_ok
        self.calls = []

    def __call__(self, cmd, timeout=None):
        self.calls.append(list(cmd))
        joined = " ".join(str(part) for part in cmd)
        if cmd[:1] == ["termux-notification-list"]:
            if self.list_error:
                return CommandResult(code=self.list_error[0], error=self.list_error[1])
            return CommandResult(code=0, stdout=json.dumps(self.notifications, ensure_ascii=False))
        if "pull" in cmd and cmd[0] == "adb":
            if not self.pull_ok or self.ui_xml is None:
                return CommandResult(code=1, stderr="pull failed")
            with open(cmd[-1], "w", encoding="utf-8") as handle:
                handle.write(self.ui_xml)
            return CommandResult(code=0)
        return CommandResult(code=0)

    def count(self, *needles):
        return len([c for c in self.calls if all(n in " ".join(c) for n in needles)])


class FakeMemory:
    def __init__(self, raises=False):
        self.turns = []
        self.raises = raises

    def remember_turn(self, text, decision, outcome="ok"):
        if self.raises:
            raise RuntimeError("db locked")
        self.turns.append({"text": text, "decision": decision, "outcome": outcome})
        return {"ok": True, "episode_id": len(self.turns)}


class FakeBrain:
    def __init__(self, decision=None, raises=False):
        self.decision = decision or {
            "action": "speak",
            "params": {"text": "বস, হ্যাঁ, কাল দেখা হবে। সময় বলবেন?"},
            "reply_bn": "বস, হ্যাঁ, কাল দেখা হবে। সময় বলবেন?",
        }
        self.raises = raises
        self.prompts = []

    def think(self, text, *args, **kwargs):
        self.prompts.append(text)
        if self.raises:
            raise RuntimeError("brain offline")
        return self.decision


class FakeAdb:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def send_whatsapp(self, **kwargs):
        self.calls.append(kwargs)
        if not self.ok:
            return {"ok": False, "error": "whatsapp not installed", "action": "whatsapp"}
        return {
            "ok": True,
            "action": "whatsapp",
            "name": kwargs.get("contact"),
            "number": "+8801712345678",
            "message": kwargs.get("message"),
            "sent": True,
        }


class FakeComm:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def send_sms(self, **kwargs):
        self.calls.append(kwargs)
        if not self.ok:
            return {"ok": False, "error": "no radio", "action": "sms"}
        return {"ok": True, "action": "sms", "message": kwargs.get("message")}


class FakeSettings:
    gemini_api_key = ""
    language = "bn-IN"


def make(notifications=None, **kwargs):
    runner = kwargs.pop("runner", None) or NotifRunner(notifications)
    memory = kwargs.pop("memory", None) or FakeMemory()
    adb = kwargs.pop("adb", None) or FakeAdb()
    comm = kwargs.pop("comm", None) or FakeComm()
    return NotificationResponder(
        memory=memory,
        adb=adb,
        comm=comm,
        runner=runner,
        settings=FakeSettings(),
        **kwargs,
    )


UI_WITH_REPLY = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" bounds="[0,0][1080,2400]">
    <node index="1" text="কাল দেখা হবে?" bounds="[40,300][700,360]" />
    <node index="2" text="Reply" content-desc="" bounds="[800,300][1000,360]" />
  </node>
</hierarchy>
"""

UI_NO_REPLY = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0"><node text="Nothing here" bounds="[0,0][100,100]" /></hierarchy>
"""


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

class TestListNotifications(unittest.TestCase):
    def test_parses_and_translates_app_names(self):
        responder = make([WHATSAPP_MSG, CHROME_DOWNLOAD])
        listing = responder.list_notifications()

        self.assertTrue(listing["ok"])
        self.assertEqual(listing["count"], 2)
        whatsapp = listing["notifications"][0]
        self.assertEqual(whatsapp["app"], "হোয়াটসঅ্যাপ")
        self.assertEqual(whatsapp["title"], "রহিম")
        self.assertEqual(whatsapp["text"], "কাল দেখা হবে?")

    def test_missing_termux_binary(self):
        runner = NotifRunner(list_error=(127, "termux-notification-list not found"))
        listing = make(runner=runner).list_notifications()
        self.assertFalse(listing["ok"])
        self.assertIn("not found", listing["error"])

    def test_bad_json(self):
        class BadRunner(NotifRunner):
            def __call__(self, cmd, timeout=None):
                if cmd[:1] == ["termux-notification-list"]:
                    return CommandResult(code=0, stdout="Warning: something\nnot json at all")
                return super().__call__(cmd, timeout)

        listing = make(runner=BadRunner()).list_notifications()
        self.assertFalse(listing["ok"])
        self.assertIn("unreadable", listing["error"])

    def test_json_after_warning_line(self):
        class NoisyRunner(NotifRunner):
            def __call__(self, cmd, timeout=None):
                if cmd[:1] == ["termux-notification-list"]:
                    return CommandResult(code=0, stdout="WARNING: x\n" + json.dumps([WHATSAPP_MSG]))
                return super().__call__(cmd, timeout)

        listing = make(runner=NoisyRunner()).list_notifications()
        self.assertTrue(listing["ok"])
        self.assertEqual(listing["count"], 1)

    def test_is_messaging(self):
        self.assertTrue(NotificationResponder.is_messaging({"package": "com.whatsapp"}))
        self.assertTrue(NotificationResponder.is_messaging({"package": "com.whatsapp.w4b"}))
        self.assertTrue(NotificationResponder.is_messaging({"package": "org.telegram.messenger"}))
        self.assertTrue(NotificationResponder.is_messaging({"package": "x", "category": "msg"}))
        self.assertFalse(NotificationResponder.is_messaging({"package": "com.android.chrome"}))


# --------------------------------------------------------------------------- #
# Dedupe + memory
# --------------------------------------------------------------------------- #

class TestPoll(unittest.TestCase):
    def test_only_messaging_and_only_once(self):
        runner = NotifRunner([WHATSAPP_MSG, CHROME_DOWNLOAD, TELEGRAM_MSG])
        memory = FakeMemory()
        responder = make(runner=runner, memory=memory)

        first = responder.poll()
        self.assertTrue(first["ok"])
        self.assertEqual(first["count"], 2)
        self.assertEqual({m["key"] for m in first["new_messages"]}, {"wa:1", "tg:1"})
        self.assertNotIn("chrome:1", {m["key"] for m in first["new_messages"]})

        second = responder.poll()
        self.assertEqual(second["count"], 0)
        self.assertEqual(second["new_messages"], [])
        self.assertIn("নতুন কোনো মেসেজ নেই", second["summary_bn"])

    def test_summary_uses_bengali_digits(self):
        result = make([WHATSAPP_MSG, TELEGRAM_MSG]).poll()
        self.assertIn("২টা", result["summary_bn"])
        self.assertIn("রহিম", result["summary_bn"])

    def test_dedupe_without_key_uses_package_and_id(self):
        item = dict(WHATSAPP_MSG)
        item.pop("key")
        responder = make([item])
        self.assertEqual(responder.poll()["count"], 1)
        self.assertEqual(responder.poll()["count"], 0)

    def test_seen_list_is_bounded(self):
        many = [dict(WHATSAPP_MSG, key=f"wa:{index}", id=str(index)) for index in range(30)]
        responder = make(many, seen_limit=10)
        responder.poll()
        self.assertLessEqual(len(responder._seen), 10)

    def test_logs_each_message_to_memory(self):
        memory = FakeMemory()
        result = make([WHATSAPP_MSG], memory=memory).poll()

        self.assertEqual(len(memory.turns), 1)
        turn = memory.turns[0]
        self.assertEqual(turn["outcome"], "notification")
        self.assertEqual(turn["decision"]["action"], "notification")
        self.assertIn("হোয়াটসঅ্যাপ", turn["text"])
        self.assertIn("রহিম", turn["text"])
        self.assertIn("কাল দেখা হবে", turn["text"])
        self.assertEqual(result["logged"][0]["episode_id"], 1)

    def test_memory_failure_is_swallowed(self):
        memory = FakeMemory(raises=True)
        result = make([WHATSAPP_MSG], memory=memory).poll()
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["logged"], [])

    def test_no_memory_engine(self):
        responder = NotificationResponder(
            memory=None, adb=FakeAdb(), comm=FakeComm(), runner=NotifRunner([WHATSAPP_MSG]), settings=FakeSettings()
        )
        result = responder.poll()
        self.assertTrue(result["ok"])
        self.assertEqual(result["logged"], [])

    def test_listing_failure_returns_quiet_summary(self):
        responder = make(runner=NotifRunner(list_error=(1, "permission denied")))
        result = responder.poll()
        self.assertFalse(result["ok"])
        self.assertEqual(result["new_messages"], [])
        self.assertIn("পড়া গেল না", result["summary_bn"])


# --------------------------------------------------------------------------- #
# Drafting and sending
# --------------------------------------------------------------------------- #

class TestReply(unittest.TestCase):
    def message(self):
        return NotificationResponder._normalise(WHATSAPP_MSG)

    def test_compose_strips_boss_prefix(self):
        brain = FakeBrain()
        drafted = make(brain=brain).compose_reply(self.message())
        self.assertTrue(drafted["ok"])
        self.assertEqual(drafted["text"], "হ্যাঁ, কাল দেখা হবে। সময় বলবেন?")
        self.assertNotIn("বস", drafted["text"])
        self.assertIn("রহিম", brain.prompts[0])
        self.assertIn("কাল দেখা হবে", brain.prompts[0])

    def test_compose_prefers_message_param(self):
        brain = FakeBrain(
            {"action": "whatsapp", "params": {"contact": "রহিম", "message": "পরে বলছি"}, "reply_bn": "বস, পাঠাচ্ছি।"}
        )
        drafted = make(brain=brain).compose_reply(self.message())
        self.assertEqual(drafted["text"], "পরে বলছি")

    def test_no_brain(self):
        drafted = make().compose_reply(self.message())
        self.assertFalse(drafted["ok"])
        self.assertIn("no brain", drafted["error"])

    def test_brain_failure(self):
        drafted = make(brain=FakeBrain(raises=True)).compose_reply(self.message())
        self.assertFalse(drafted["ok"])

    def test_reply_whatsapp_uses_deep_link(self):
        adb = FakeAdb()
        result = make(adb=adb).reply_to(self.message(), "আসছি")
        self.assertTrue(result["ok"])
        self.assertEqual(result["via"], "whatsapp")
        self.assertEqual(adb.calls[0]["contact"], "রহিম")
        self.assertEqual(adb.calls[0]["message"], "আসছি")
        self.assertTrue(adb.calls[0]["press_send"])

    def test_reply_sms_uses_comm_manager(self):
        comm = FakeComm()
        message = NotificationResponder._normalise(SMS_CLEAN)
        result = make(comm=comm).reply_to(message, "ঠিক আছে")
        self.assertTrue(result["ok"])
        self.assertEqual(result["via"], "sms")
        self.assertEqual(comm.calls[0]["contact"], "রহিম")

    def test_reply_telegram_needs_manual(self):
        adb, comm = FakeAdb(), FakeComm()
        message = NotificationResponder._normalise(TELEGRAM_MSG)
        result = make(adb=adb, comm=comm).reply_to(message, "ok")
        self.assertFalse(result["ok"])
        self.assertTrue(result["needs_manual"])
        self.assertIn("টেলিগ্রাম", result["message_bn"])
        self.assertEqual(adb.calls, [])
        self.assertEqual(comm.calls, [])

    def test_reply_without_text(self):
        result = make().reply_to(self.message())
        self.assertFalse(result["ok"])
        self.assertIn("উত্তর", result["error"])

    def test_poll_auto_reply_sends(self):
        brain = FakeBrain()
        adb = FakeAdb()
        result = make([WHATSAPP_MSG], brain=brain, adb=adb, auto_reply=True).poll()

        self.assertEqual(len(result["replies"]), 1)
        self.assertTrue(result["replies"][0]["ok"])
        self.assertEqual(adb.calls[0]["message"], "হ্যাঁ, কাল দেখা হবে। সময় বলবেন?")

    def test_poll_auto_reply_without_brain_reports_manual(self):
        result = make([WHATSAPP_MSG], auto_reply=True).poll()
        self.assertEqual(len(result["replies"]), 1)
        self.assertFalse(result["replies"][0]["ok"])
        self.assertTrue(result["replies"][0]["needs_manual"])

    def test_poll_auto_reply_override(self):
        adb = FakeAdb()
        responder = make([WHATSAPP_MSG], adb=adb, brain=FakeBrain(), auto_reply=False)
        result = responder.poll(auto_reply=True)
        self.assertTrue(result["auto_reply"])
        self.assertEqual(len(adb.calls), 1)


# --------------------------------------------------------------------------- #
# Financial / OTP guard
# --------------------------------------------------------------------------- #

class TestSecurityFilter(unittest.TestCase):
    def test_otp_sms_is_never_returned_logged_or_answered(self):
        memory = FakeMemory()
        adb = FakeAdb()
        responder = make([SMS_MSG], memory=memory, adb=adb, brain=FakeBrain(), auto_reply=True)

        result = responder.poll()

        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["new_messages"], [])
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(result["replies"], [])
        self.assertEqual(memory.turns, [])
        self.assertEqual(adb.calls, [])
        self.assertIn("নতুন কোনো মেসেজ নেই", result["summary_bn"])

    def test_bank_transaction_alert_is_blocked(self):
        memory = FakeMemory()
        result = make([BANK_ALERT], memory=memory).poll()
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(memory.turns, [])

    def test_blocked_notification_is_not_reported_twice(self):
        responder = make([SMS_MSG])
        self.assertEqual(responder.poll()["blocked"], 1)
        second = responder.poll()
        self.assertEqual(second["blocked"], 0)
        self.assertEqual(second["count"], 0)

    def test_log_messages_skips_sensitive_direct_calls(self):
        memory = FakeMemory()
        responder = make(memory=memory)
        result = responder.log_messages([NotificationResponder._normalise(SMS_MSG)])

        self.assertEqual(result["logged"], [])
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(memory.turns, [])

    def test_compose_reply_refuses_sensitive_message_before_the_brain(self):
        brain = FakeBrain()
        responder = make(brain=brain)
        drafted = responder.compose_reply(NotificationResponder._normalise(SMS_MSG))

        self.assertFalse(drafted["ok"])
        self.assertEqual(drafted["error"], "sensitive_notification_blocked")
        self.assertIn("ওটিপি", drafted["reply_bn"])
        self.assertEqual(brain.prompts, [])

    def test_reply_to_sensitive_message_sends_nothing(self):
        adb, comm = FakeAdb(), FakeComm()
        responder = make(adb=adb, comm=comm)
        result = responder.reply_to(NotificationResponder._normalise(BANK_ALERT), "ঠিক আছে")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "sensitive_notification_blocked")
        self.assertEqual(adb.calls, [])
        self.assertEqual(comm.calls, [])

    def test_reply_text_with_a_secret_is_refused(self):
        adb = FakeAdb()
        responder = make(adb=adb)
        result = responder.reply_to(
            NotificationResponder._normalise(WHATSAPP_MSG), "আমার OTP 445566"
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "secret_message_blocked")
        self.assertEqual(adb.calls, [])

    def test_normal_message_still_works_alongside_a_blocked_one(self):
        memory = FakeMemory()
        result = make([WHATSAPP_MSG, SMS_MSG], memory=memory).poll()

        self.assertEqual({m["key"] for m in result["new_messages"]}, {"wa:1"})
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(len(memory.turns), 1)


# --------------------------------------------------------------------------- #
# Optional inline reply
# --------------------------------------------------------------------------- #

class TestInline(unittest.TestCase):
    def test_ascii_inline_taps_reply_and_types(self):
        runner = NotifRunner(ui_xml=UI_WITH_REPLY)
        responder = make(runner=runner, use_inline=True)

        result = responder.inline_reply("ok see you")

        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["bounds"], (900, 330))
        self.assertTrue(runner.count("input", "tap", "900", "330"))
        self.assertTrue(runner.count("input", "text", "ok%ssee%syou"))
        self.assertTrue(runner.count("input", "keyevent", "66"))

    def test_bengali_is_skipped(self):
        responder = make(use_inline=True)
        result = responder.inline_reply("আসছি")
        self.assertFalse(result["ok"])
        self.assertEqual(result["skipped"], "non-ascii")

    def test_no_reply_button(self):
        responder = make(runner=NotifRunner(ui_xml=UI_NO_REPLY), use_inline=True)
        result = responder.inline_reply("hello")
        self.assertFalse(result["ok"])
        self.assertIn("no Reply button", result["error"])

    def test_inline_failure_falls_back_to_whatsapp(self):
        adb = FakeAdb()
        responder = make(runner=NotifRunner(ui_xml=UI_NO_REPLY), adb=adb, use_inline=True)
        result = responder.reply_to(NotificationResponder._normalise(WHATSAPP_MSG), "hello")

        self.assertTrue(result["ok"])
        self.assertEqual(result["via"], "whatsapp")
        self.assertEqual(len(adb.calls), 1)
        self.assertEqual(result["attempts"][0]["via"], "inline")

    def test_dump_failure(self):
        responder = make(runner=NotifRunner(pull_ok=False, ui_xml=UI_WITH_REPLY), use_inline=True)
        result = responder.inline_reply("hello")
        self.assertFalse(result["ok"])


class TestParseBounds(unittest.TestCase):
    def test_exact_reply_wins_over_paragraph(self):
        xml = (
            '<hierarchy><node text="Please reply to him later" bounds="[0,0][100,100]" />'
            '<node text="Reply" bounds="[10,20][30,40]" /></hierarchy>'
        )
        self.assertEqual(NotificationResponder._reply_bounds(xml), (20, 30))

    def test_broken_xml(self):
        self.assertIsNone(NotificationResponder._reply_bounds("<hierarchy><node"))

    def test_missing_bounds(self):
        xml = '<hierarchy><node text="Reply" /></hierarchy>'
        self.assertIsNone(NotificationResponder._reply_bounds(xml))


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
