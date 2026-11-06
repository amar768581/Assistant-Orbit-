"""
tests/test_telegram_controller.py — admin gating, commands, voice notes and
long polling against a fake Bot API (no network, no real token).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from telegram_controller import (
    DOWNLOAD_FAIL_BN,
    REFUSAL_BN,
    TRANSCRIBE_FAIL_BN,
    TelegramController,
)


class FakeResponse:
    def __init__(self, payload=None, status=200, content=b"", text=""):
        self._payload = payload if payload is not None else {"ok": True, "result": True}
        self.status_code = status
        self.content = content
        self.text = text or str(self._payload)

    def json(self):
        return self._payload


class FakeAPI:
    """Records every Bot API call; routes getUpdates/getFile automatically."""

    def __init__(self, updates=None, file_content=b"OggS-fake-audio"):
        self.calls = []
        self.gets = []
        self.updates = list(updates or [])
        self.file_content = file_content
        self.fail = {}

    def post(self, url, json=None, data=None, files=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append({"method": method, "json": json, "data": data, "files": files})
        if method in self.fail:
            return FakeResponse({"ok": False, "description": "boom"}, status=self.fail[method])
        if method == "getUpdates":
            batch = self.updates.pop(0) if self.updates else []
            return FakeResponse({"ok": True, "result": batch})
        if method == "getFile":
            return FakeResponse({"ok": True, "result": {"file_path": "voice/file_1.ogg"}})
        if method in ("sendMessage", "sendPhoto", "sendVoice"):
            return FakeResponse({"ok": True, "result": {"message_id": len(self.calls)}})
        return FakeResponse({"ok": True, "result": True})

    def get(self, url, timeout=None):
        self.gets.append(url)
        return FakeResponse(status=200, content=self.file_content)

    # helpers
    def sent_texts(self):
        return [
            c["json"]["text"] for c in self.calls
            if c["method"] == "sendMessage" and c.get("json")
        ]

    def methods(self):
        return [c["method"] for c in self.calls]


class FakeDispatcher:
    def __init__(self, outcome=None):
        self.outcome = outcome or {"ok": True, "reply_bn": "বস, কাজ শেষ।", "action": "torch", "result": {}}
        self.texts = []

    def process(self, text):
        self.texts.append(text)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return dict(self.outcome)


class FakeMemory:
    def __init__(self):
        self.upgraded = 0

    def stats(self):
        return {"ok": True, "episodes": 12, "facts": 3, "rules": 2, "notes": 1}

    def daily_self_upgrade(self):
        self.upgraded += 1
        return {
            "ok": True,
            "rule": {"rule_text": "ব্যর্থ হলে বিকল্প উপায়ে চেষ্টা করব।"},
            "insight_bn": "বারবার বাধা আসছে, তাই আগে পারমিশন যাচাই দরকার।",
            "question_bn": "আজ কোন কাজটা আগে সহজ করা যায়?",
        }


class FakeVoice:
    def __init__(self, transcript="টর্চ জালাও", synth_path=None, raises=False):
        self.transcript = transcript
        self.synth_path = synth_path
        self.raises = raises
        self.transcribed = []

    def transcribe_file(self, path):
        if self.raises:
            raise RuntimeError("whisper down")
        self.transcribed.append(Path(path))
        return self.transcript

    def synthesize(self, text):
        return self.synth_path


class FakeBriefing:
    def __init__(self):
        self.calls = 0

    def market_news(self):
        self.calls += 1
        return {"ok": True, "reply_bn": "বস, আজ নিফটি বেড়েছে এবং বিশ্বে বাণিজ্য আলোচনা চলছে।"}


class FakeSecurity:
    def __init__(self, photo=None):
        self.photo = photo
        self.calls = 0

    def intruder_check(self):
        self.calls += 1
        return {
            "ok": True,
            "photo_path": str(self.photo) if self.photo else None,
            "maps_url": "https://maps.google.com/?q=23.81,90.41",
            "summary_bn": "ছবি তোলা হয়েছে, লোকেশন পাওয়া গেছে।",
        }


class FakeControl:
    def __init__(self, ok=True):
        self.ok = ok

    def battery_status(self):
        if not self.ok:
            return {"ok": False, "error": "no termux-api"}
        return {"ok": True, "percentage": 87, "message_bn": "ব্যাটারি ৮৭ শতাংশ, চার্জ কমছে।"}


class FakeReceptionist:
    def __init__(self):
        self.auto_reception = False


def message(text=None, chat_id=42, **extra):
    payload = {"chat": {"id": chat_id}, "from": {"id": chat_id}}
    if text is not None:
        payload["text"] = text
    payload.update(extra)
    return {"update_id": 1, "message": payload}


def outcome(reply="বস, কাজ শেষ।", action="torch", ok=True, result=None):
    return {"ok": ok, "reply_bn": reply, "action": action, "result": result or {}}


class TelegramCase(unittest.TestCase):
    token = "123:test"
    admin = 42

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.api = FakeAPI()
        self.dispatcher = FakeDispatcher()
        self.memory = FakeMemory()
        self.voice = FakeVoice()
        self.briefing = FakeBriefing()
        self.security = FakeSecurity()
        self.control = FakeControl()
        self.receptionist = FakeReceptionist()
        self.settings = SimpleNamespace(
            telegram_bot_token=self.token,
            admin_chat_id=self.admin,
            gemini_api_key="",
            groq_api_key="",
        )
        self.controller = self.make()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, **kwargs):
        params = dict(
            dispatcher=self.dispatcher,
            settings=self.settings,
            session=self.api,
            memory=self.memory,
            voice=self.voice,
            briefing=self.briefing,
            security=self.security,
            control=self.control,
            receptionist=self.receptionist,
            audio_dir=self.dir,
            sleep=lambda seconds: None,
        )
        params.update(kwargs)
        return TelegramController(**params)

    def sent(self):
        return self.api.sent_texts()


# --------------------------------------------------------------------------- #
# Security gate
# --------------------------------------------------------------------------- #

class TestSecurity(TelegramCase):
    def test_unauthorized_chat_is_refused_and_never_dispatched(self):
        with self.assertLogs("orbit.telegram", level="WARNING") as logs:
            result = self.controller.handle_update(message("টর্চ জালাও", chat_id=99))

        self.assertFalse(result["ok"])
        self.assertFalse(result["authorized"])
        self.assertEqual(self.dispatcher.texts, [])
        self.assertTrue(result["kind"] == "refused")
        self.assertIn(REFUSAL_BN, self.sent())
        self.assertTrue(any("SECURITY" in line and "99" in line for line in logs.output))

    def test_second_message_from_same_stranger_is_not_answered_again(self):
        self.controller.handle_update(message("hi", chat_id=99))
        self.controller.handle_update(message("hello again", chat_id=99))
        self.assertEqual(len([t for t in self.sent() if t == REFUSAL_BN]), 1)

    def test_missing_admin_chat_id_refuses_everyone(self):
        controller = self.make(settings=SimpleNamespace(
            telegram_bot_token=self.token, admin_chat_id=None,
            gemini_api_key="", groq_api_key="",
        ))
        result = controller.handle_update(message("টর্চ জালাও", chat_id=42))
        self.assertFalse(result["authorized"])
        self.assertEqual(self.dispatcher.texts, [])

    def test_authorized_chat_runs_the_dispatcher(self):
        result = self.controller.handle_update(message("টর্চ জালাও"))
        self.assertTrue(result["authorized"])
        self.assertEqual(self.dispatcher.texts, ["টর্চ জালাও"])
        self.assertIn("বস, কাজ শেষ।", self.sent())

    def test_missing_token_makes_no_http_call(self):
        controller = self.make(settings=SimpleNamespace(
            telegram_bot_token="", admin_chat_id=42, gemini_api_key="", groq_api_key="",
        ))
        sent = controller.send_message(42, "বস, টেস্ট")
        self.assertFalse(sent["ok"])
        self.assertEqual(self.api.calls, [])
        self.assertFalse(controller.diagnostics()["token"])

    def test_transport_error_is_returned_not_raised(self):
        self.api.fail["sendMessage"] = 500
        sent = self.controller.send_message(42, "বস, টেস্ট")
        self.assertFalse(sent["ok"])
        self.assertIn("500", self.controller.last_error)


# --------------------------------------------------------------------------- #
# Text + media
# --------------------------------------------------------------------------- #

class TestText(TelegramCase):
    def test_intruder_result_uploads_photo_with_maps_caption(self):
        photo = self.dir / "intruder.jpg"
        photo.write_bytes(b"\xff\xd8\xff" + b"x" * 2000)
        self.dispatcher.outcome = outcome(
            "বস, ছবি তুলেছি।", action="intruder_check",
            result={"photo_path": str(photo), "maps_url": "https://maps.google.com/?q=1,2"},
        )

        self.controller.handle_update(message("কে আছে দেখো"))

        uploads = [c for c in self.api.calls if c["method"] == "sendPhoto"]
        self.assertEqual(len(uploads), 1)
        self.assertIn("maps.google.com", uploads[0]["data"]["caption"])
        self.assertFalse(any("map" in text for text in self.sent()))

    def test_maps_link_is_appended_to_text_reply(self):
        self.dispatcher.outcome = outcome(
            "বস, লোকেশন পাওয়া গেছে।", action="read_screen",
            result={"maps_url": "https://maps.google.com/?q=1,2"},
        )
        self.controller.handle_update(message("স্ক্রিনে কী আছে"))
        self.assertIn("maps.google.com", self.sent()[-1])

    def test_photo_path_that_does_not_exist_falls_back_to_text(self):
        self.dispatcher.outcome = outcome(
            "বস, ছবি নেই।", action="intruder_check",
            result={"photo_path": str(self.dir / "missing.jpg")},
        )
        self.controller.handle_update(message("কে আছে"))
        self.assertNotIn("sendPhoto", self.api.methods())
        self.assertIn("বস, ছবি নেই।", self.sent()[-1])

    def test_dispatcher_crash_is_contained(self):
        self.dispatcher.outcome = RuntimeError("kaboom")
        result = self.controller.handle_update(message("কিছু করো"))
        self.assertFalse(result["ok"])
        self.assertTrue(self.sent()[-1].startswith("বস"))

    def test_empty_text_is_not_dispatched(self):
        result = self.controller.handle_text("   ", 42)
        self.assertFalse(result["ok"])
        self.assertEqual(self.dispatcher.texts, [])
        self.assertTrue(self.sent())

    def test_otp_from_boss_is_refused_before_dispatch(self):
        result = self.controller.handle_update(message("আমার OTP 445566 মনে রাখো"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "blocked")
        self.assertEqual(self.dispatcher.texts, [])
        self.assertIn("ওটিপি", result["reply_bn"])
        self.assertIn(result["reply_bn"], self.sent())

    def test_bank_app_request_is_refused_before_dispatch(self):
        for text in ("SBI YONO খোলো", "PhonePe দিয়ে টাকা পাঠাও", "ব্যাংক অ্যাপে লগইন করো"):
            with self.subTest(text=text):
                result = self.controller.handle_text(text, 42)
                self.assertFalse(result["ok"])
                self.assertEqual(result["kind"], "blocked")
                self.assertIn("ব্যাংকিং", result["reply_bn"])
        self.assertEqual(self.dispatcher.texts, [])

    def test_voice_transcript_with_a_secret_is_refused(self):
        self.voice.transcript = "আমার পিন ১২৩৪ মনে রাখো"
        result = self.controller.handle_update(message(voice={"file_id": "v-9"}))

        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "voice")
        self.assertEqual(self.dispatcher.texts, [])

    def test_voice_note_is_downloaded_transcribed_and_dispatched(self):
        result = self.controller.handle_update(
            message(voice={"file_id": "v-1", "duration": 4})
        )

        self.assertEqual(result["kind"], "voice")
        self.assertEqual(result["transcript"], "টর্চ জালাও")
        self.assertEqual(self.dispatcher.texts, ["টর্চ জালাও"])
        self.assertIn("getFile", self.api.methods())
        self.assertTrue(self.api.gets and "file_1.ogg" in self.api.gets[-1])
        self.assertEqual(len(self.voice.transcribed), 1)
        self.assertTrue(self.voice.transcribed[0].is_file())
        self.assertTrue(any("🎧 শুনলাম" in text for text in self.sent()))
        self.assertTrue(any("বস, কাজ শেষ।" in text for text in self.sent()))

    def test_audio_message_works_too(self):
        result = self.controller.handle_update(message(audio={"file_id": "a-1"}))
        self.assertEqual(result["transcript"], "টর্চ জালাও")
        self.assertEqual(self.dispatcher.texts, ["টর্চ জালাও"])

    def test_transcription_failure_is_reported(self):
        self.voice.raises = True
        result = self.controller.handle_update(message(voice={"file_id": "v-1"}))
        self.assertFalse(result["ok"])
        self.assertEqual(self.dispatcher.texts, [])
        self.assertIn(TRANSCRIBE_FAIL_BN, self.sent())

    def test_download_failure_is_reported(self):
        self.api.fail["getFile"] = 404
        result = self.controller.handle_update(message(voice={"file_id": "v-1"}))
        self.assertFalse(result["ok"])
        self.assertIn(DOWNLOAD_FAIL_BN, self.sent())
        self.assertEqual(self.dispatcher.texts, [])

    def test_empty_transcript_is_reported(self):
        self.voice.transcript = ""
        result = self.controller.handle_update(message(voice={"file_id": "v-1"}))
        self.assertFalse(result["ok"])
        self.assertIn(TRANSCRIBE_FAIL_BN, self.sent())

    def test_voice_reply_sends_an_audio_message(self):
        audio = self.dir / "reply.mp3"
        audio.write_bytes(b"ID3" + b"x" * 800)
        controller = self.make(reply_voice=True)
        controller.voice.synth_path = audio

        controller.handle_update(message("টর্চ জালাও"))

        self.assertIn("sendVoice", self.api.methods())

    def test_update_without_message_is_ignored(self):
        result = self.controller.handle_update({"update_id": 5, "callback_query": {}})
        self.assertEqual(result["kind"], "ignored")
        self.assertEqual(self.dispatcher.texts, [])


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

class TestCommands(TelegramCase):
    def command(self, text, chat_id=42):
        return self.controller.handle_update(message(text, chat_id=chat_id))

    def test_help_and_start(self):
        for text in ("/help", "/start", "/help@OrbitBot"):
            result = self.command(text)
            self.assertTrue(result["ok"], text)
            self.assertIn("Orbit", result["reply_bn"])
            self.assertTrue(result["reply_bn"].startswith("বস"))
            self.assertIn("বাজার", result["reply_bn"])
        self.assertEqual(self.dispatcher.texts, [])

    def test_status_reports_battery_memory_and_services(self):
        result = self.command("/status")
        reply = result["reply_bn"]
        self.assertIn("৮৭", reply)
        self.assertIn("মেমোরি", reply)
        self.assertIn("১২", reply)
        self.assertIn("সার্ভিস", reply)
        self.assertIn("Telegram", reply)
        self.assertIn("ব্যাংকিং লক", reply)
        self.assertIn("OTP", reply)

    def test_status_survives_broken_modules(self):
        controller = self.make(control=FakeControl(ok=False))
        controller.memory = None
        result = controller.handle_command("/status", 42)
        self.assertTrue(result["ok"])
        self.assertIn("পাওয়া যায়নি", result["reply_bn"])
        self.assertIn("সংযুক্ত নয়", result["reply_bn"])

    def test_status_uses_injected_status_provider(self):
        controller = self.make(status_provider=lambda: {"টেলিগ্রাম": True, "ব্রিফিং": False})
        reply = controller.handle_command("/status", 42)["reply_bn"]
        self.assertIn("টেলিগ্রাম ✅", reply)
        self.assertIn("ব্রিফিং ⏸️", reply)

    def test_briefing_command(self):
        result = self.command("/briefing")
        self.assertEqual(self.briefing.calls, 1)
        self.assertIn("নিফটি", result["reply_bn"])
        self.assertEqual(self.dispatcher.texts, [])

    def test_thinker_command(self):
        result = self.command("/thinker")
        self.assertEqual(self.memory.upgraded, 1)
        self.assertIn("শেখা", result["reply_bn"])
        self.assertIn("উপলব্ধি", result["reply_bn"])
        self.assertIn("প্রশ্ন", result["reply_bn"])
        self.assertTrue(result["reply_bn"].startswith("বস"))

    def test_thinker_without_memory(self):
        self.controller.memory = None
        result = self.controller.handle_command("/thinker", 42)
        self.assertIn("মেমোরি", result["reply_bn"])

    def test_intruder_command_sends_photo(self):
        photo = self.dir / "intruder.jpg"
        photo.write_bytes(b"\xff\xd8\xff" + b"y" * 2000)
        self.security.photo = photo

        result = self.command("/intruder")

        self.assertEqual(self.security.calls, 1)
        self.assertTrue(result["photo"])
        uploads = [c for c in self.api.calls if c["method"] == "sendPhoto"]
        self.assertEqual(len(uploads), 1)
        self.assertIn("maps.google.com", uploads[0]["data"]["caption"])

    def test_receptionist_toggle(self):
        on = self.command("/receptionist on")
        self.assertTrue(self.receptionist.auto_reception)
        self.assertIn("চালু", on["reply_bn"])

        off = self.command("/receptionist off")
        self.assertFalse(self.receptionist.auto_reception)
        self.assertIn("বন্ধ", off["reply_bn"])

        status = self.command("/receptionist")
        self.assertIn("বন্ধ", status["reply_bn"])

    def test_receptionist_without_module(self):
        self.controller.receptionist = None
        result = self.controller.handle_command("/receptionist on", 42)
        self.assertIn("সংযুক্ত নেই", result["reply_bn"])

    def test_unknown_command(self):
        result = self.command("/nonsense")
        self.assertIn("চিনি না", result["reply_bn"])
        self.assertEqual(self.dispatcher.texts, [])
        self.assertEqual(len(self.api.calls), 1)

    def test_command_does_not_execute_device_actions(self):
        self.command("/status")
        self.assertEqual(self.dispatcher.texts, [])


# --------------------------------------------------------------------------- #
# Push helpers + chunking
# --------------------------------------------------------------------------- #

class TestPushAndChunking(TelegramCase):
    def test_notify_boss(self):
        sent = self.controller.notify_boss("বস, কাজ শেষ হয়েছে।")
        self.assertTrue(sent["ok"])
        self.assertEqual(self.api.calls[-1]["json"]["chat_id"], 42)
        self.assertEqual(self.api.calls[-1]["json"]["text"], "বস, কাজ শেষ হয়েছে।")

    def test_notify_boss_without_admin(self):
        controller = self.make(settings=SimpleNamespace(
            telegram_bot_token=self.token, admin_chat_id=None,
            gemini_api_key="", groq_api_key="",
        ))
        self.assertFalse(controller.notify_boss("হ্যালো")["ok"])

    def test_call_summary(self):
        sent = self.controller.send_call_summary({
            "ok": True,
            "caller": "রহিম",
            "summary_bn": "রহিম জরুরি কাজে কল করেছিলেন।",
            "transcript_text": "কাল সকালে দেখা করতে বলেছেন",
        })
        self.assertTrue(sent["ok"])
        text = self.sent()[-1]
        self.assertIn("রহিম", text)
        self.assertIn("জরুরি", text)
        self.assertIn("কাল সকালে", text)

    def test_intruder_alert_without_photo_is_text_with_maps(self):
        sent = self.controller.send_intruder_alert({
            "summary_bn": "লোকেশন পাওয়া গেছে।",
            "maps_url": "https://maps.google.com/?q=9,8",
            "photo_path": None,
        })
        self.assertTrue(sent["ok"])
        self.assertFalse(sent["photo"])
        self.assertIn("maps.google.com", self.sent()[-1])

    def test_split_message_respects_the_limit(self):
        text = "\n".join(f"লাইন {i} — " + "ক" * 300 for i in range(30))
        chunks = TelegramController.split_message(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 4000 for chunk in chunks))
        self.assertEqual("\n".join(chunks), text.strip())

    def test_split_message_hard_splits_one_huge_line(self):
        chunks = TelegramController.split_message("ক" * 9000)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(chunk) <= 4000 for chunk in chunks))

    def test_long_reply_is_sent_in_parts(self):
        self.dispatcher.outcome = outcome("বস, " + "খ" * 9000)
        self.controller.handle_update(message("লম্বা উত্তর দাও"))
        texts = self.sent()
        self.assertGreaterEqual(len(texts), 3)
        self.assertTrue(all(len(text) <= 4000 for text in texts))


# --------------------------------------------------------------------------- #
# Polling
# --------------------------------------------------------------------------- #

class TestPolling(TelegramCase):
    def test_poll_once_handles_batch_and_advances_offset(self):
        self.api.updates = [[
            {"update_id": 10, "message": {"chat": {"id": 42}, "text": "টর্চ জালাও"}},
            {"update_id": 11, "message": {"chat": {"id": 99}, "text": "হ্যাক"}},
        ]]
        result = self.controller.poll_once()

        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["offset"], 12)
        self.assertEqual(self.dispatcher.texts, ["টর্চ জালাও"])

        # The next long poll sends the advanced offset, so Telegram drops the batch.
        self.controller.poll_once()
        polls = [c for c in self.api.calls if c["method"] == "getUpdates"]
        self.assertEqual(polls[-1]["json"]["offset"], 12)

    def test_poll_once_http_error(self):
        self.api.fail["getUpdates"] = 502
        result = self.controller.poll_once()
        self.assertFalse(result["ok"])
        self.assertIn("502", result["error"])

    def test_poll_forever_stops_on_event(self):
        import threading

        stop = threading.Event()
        stop.set()
        result = self.controller.poll_forever(stop_event=stop)
        self.assertTrue(result["ok"])
        self.assertEqual(result["polls"], 0)

    def test_poll_forever_max_polls(self):
        result = self.controller.poll_forever(max_polls=2)
        self.assertEqual(result["polls"], 2)
        self.assertEqual(result["handled"], 0)

    def test_poll_forever_handles_updates_and_counts(self):
        self.api.updates = [[
            {"update_id": 1, "message": {"chat": {"id": 42}, "text": "টর্চ জালাও"}},
        ]]
        seen = []
        result = self.controller.poll_forever(max_polls=1, on_update=seen.append)
        self.assertEqual(result["handled"], 1)
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.dispatcher.texts, ["টর্চ জালাও"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
