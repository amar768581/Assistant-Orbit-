"""
tests/test_main.py — wake-lock, service supervisor, CLI and doctor, all with
fakes: no Termux binaries, no threads left running, no real memory DB.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from main import (
    OrbitDaemon,
    WakeLock,
    build_parser,
    doctor_report,
    interactive_loop,
    main,
    print_doctor,
)
from system_control import CommandResult


class FakeRunner:
    def __init__(self, output="", code=0, devices=False):
        self.calls = []
        self.output = output
        self.code = code
        self.devices = devices

    def __call__(self, cmd, timeout=None):
        self.calls.append(list(cmd))
        if cmd[:1] == ["adb"] and self.devices:
            return CommandResult(0, "List of devices attached\nR58M12345\tdevice\n")
        return CommandResult(self.code, self.output)


class FakeSettings:
    def __init__(self, wake_lock=True, telegram_ready=False, **kwargs):
        self.wake_lock = wake_lock
        self.telegram_ready = telegram_ready
        self.memory_db = Path(tempfile.gettempdir()) / "orbit_test_memory.db"
        self.env_path = Path("/nonexistent/.env")
        self.gemini_api_key = ""
        self.groq_api_key = ""
        self.telegram_bot_token = ""
        self.admin_chat_id = None
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeControl:
    def __init__(self, alert=None, percentage=87):
        self.alert = alert
        self.percentage = percentage
        self.calls = 0

    def check_battery(self, notify=True):
        self.calls += 1
        result = {"ok": True, "percentage": self.percentage, "status": "DISCHARGING"}
        if self.alert:
            result["alert_message_bn"] = self.alert
            result["spoken"] = True
        return result

    def battery_status(self):
        return {"ok": True, "percentage": self.percentage, "message_bn": "ব্যাটারি ঠিক আছে।"}


class FakeMemory:
    def __init__(self):
        self.upgrades = 0

    def daily_self_upgrade(self):
        self.upgrades += 1
        return {
            "ok": True,
            "rule": {"rule_text": "ব্যর্থ হলে আবার চেষ্টা করব।"},
            "insight_bn": "পারমিশন আগে যাচাই করা দরকার।",
            "question_bn": "আজ কোন কাজটা সহজ করা যায়?",
        }

    def stats(self):
        return {"ok": True, "episodes": 4, "facts": 2, "rules": 1, "notes": 1}


class FakeBriefing:
    def __init__(self):
        self.calls = 0

    def market_news(self):
        self.calls += 1
        return {"ok": True, "reply_bn": "বস, নিফটি বেড়েছে।"}


class FakeVoice:
    def __init__(self, transcript="টর্চ জালাও"):
        self.transcript = transcript
        self.spoken = []

    def speak(self, text, **kwargs):
        self.spoken.append(text)
        return True

    def listen(self, seconds=6.0):
        return self.transcript


class FakeDispatcher:
    def __init__(self, result=None):
        self.result = result or {"ok": True, "reply_bn": "বস, টর্চ জ্বালালাম।", "result": {}}
        self.texts = []

    def process(self, text):
        self.texts.append(text)
        return dict(self.result)


class FakeReceptionist:
    def __init__(self, answered=None):
        self.auto_reception = False
        self.answered = answered
        self.polls = 0

    def monitor_once(self):
        self.polls += 1
        if self.answered:
            return self.answered
        return {"ok": True, "state": "idle", "answered": False}


class FakeResponder:
    def __init__(self, messages=None, replies=None):
        self.messages = messages or []
        self.replies = replies or []

    def poll(self):
        return {
            "ok": True,
            "new_messages": self.messages,
            "replies": self.replies,
            "count": len(self.messages),
        }


class FakeTelegram:
    token = "123:test"
    admin_chat_id = 42

    def __init__(self):
        self.pushes = []
        self.call_summaries = []
        self.stopped = False

    def notify_boss(self, text, photo=None):
        self.pushes.append(text)
        return {"ok": True}

    def send_call_summary(self, result):
        self.call_summaries.append(result)
        return {"ok": True}

    def poll_forever(self, stop_event=None, **kwargs):
        self.stopped = True
        return {"ok": True, "polls": 0}

    def diagnostics(self):
        return {"ok": True, "token": True, "admin_chat_id": 42}


def make_daemon(**overrides):
    settings = overrides.pop("settings", FakeSettings())
    fakes = {
        "control": FakeControl(),
        "memory": FakeMemory(),
        "brain": object(),
        "voice": FakeVoice(),
        "dispatcher": FakeDispatcher(),
        "briefing": FakeBriefing(),
        "security": object(),
        "receptionist": FakeReceptionist(),
        "responder": FakeResponder(),
        "telegram": FakeTelegram(),
        "wake_lock": WakeLock(settings=FakeSettings(), runner=FakeRunner()),
    }
    fakes.update(overrides)
    daemon = OrbitDaemon(settings=settings, restart_delay=0, **fakes)
    return daemon, fakes


# --------------------------------------------------------------------------- #
# Wake lock
# --------------------------------------------------------------------------- #

class TestWakeLock(unittest.TestCase):
    def test_acquire_and_release_are_idempotent(self):
        runner = FakeRunner()
        lock = WakeLock(settings=FakeSettings(), runner=runner)

        self.assertTrue(lock.acquire()["ok"])
        self.assertTrue(lock.held)
        self.assertTrue(lock.acquire()["skipped"] == "already held")
        self.assertEqual(runner.calls, [["termux-wake-lock"]])

        self.assertTrue(lock.release()["ok"])
        self.assertFalse(lock.held)
        self.assertEqual(lock.release()["skipped"], "not held")
        self.assertEqual(runner.calls, [["termux-wake-lock"], ["termux-wake-unlock"]])

    def test_disabled_by_settings(self):
        runner = FakeRunner()
        lock = WakeLock(settings=FakeSettings(wake_lock=False), runner=runner)
        self.assertEqual(lock.acquire()["skipped"], "WAKE_LOCK=false")
        self.assertEqual(runner.calls, [])

    def test_failed_binary_is_not_held(self):
        lock = WakeLock(settings=FakeSettings(), runner=FakeRunner(code=127))
        self.assertFalse(lock.acquire()["ok"])
        self.assertFalse(lock.held)

    def test_runner_exception_is_swallowed(self):
        def broken(cmd):
            raise OSError("boom")

        lock = WakeLock(settings=FakeSettings(), runner=broken)
        self.assertFalse(lock.acquire()["ok"])
        self.assertFalse(lock.held)


# --------------------------------------------------------------------------- #
# Supervisor
# --------------------------------------------------------------------------- #

class TestSupervisor(unittest.TestCase):
    def test_start_and_shutdown_manage_wake_lock_and_threads(self):
        daemon, fakes = make_daemon()
        daemon.register_service("test", lambda: daemon.services["test"].stop.wait(5))
        result = daemon.start(enabled=["test"])

        self.assertTrue(result["ok"])
        self.assertIn("test", result["services"]["started"])
        self.assertTrue(daemon.wake_lock.held)
        self.assertTrue(daemon.status()["services"]["test"]["running"])

        stopped = daemon.shutdown()
        self.assertTrue(stopped["ok"])
        self.assertFalse(daemon.wake_lock.held)
        self.assertFalse(daemon.status()["services"]["test"]["running"])

    def test_crashed_service_is_restarted(self):
        daemon, _ = make_daemon()
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            daemon.services["flaky"].stop.set()

        daemon.register_service("flaky", flaky)
        daemon.start_all(enabled=["flaky"])
        daemon.services["flaky"].thread.join(3)

        self.assertEqual(daemon.services["flaky"].restarts, 1)
        self.assertEqual(daemon.services["flaky"].runs, 2)
        self.assertIn("boom", daemon.services["flaky"].last_error)

    def test_telegram_service_skipped_without_configuration(self):
        daemon, _ = make_daemon(settings=FakeSettings(telegram_ready=False))
        daemon.build_services(enabled=["telegram"])
        result = daemon.start_all(enabled=["telegram"])

        self.assertEqual(result["started"], [])
        self.assertIn("telegram", result["skipped"])
        self.assertIsNone(daemon.services["telegram"].thread)

    def test_telegram_loop_starts_when_configured(self):
        daemon, fakes = make_daemon(settings=FakeSettings(telegram_ready=True))
        daemon.register_service("telegram", daemon._telegram_loop)
        daemon.start_all(enabled=["telegram"])
        daemon.stop_all()
        self.assertTrue(fakes["telegram"].stopped)

    def test_service_status_uses_bengali_names(self):
        daemon, _ = make_daemon()
        daemon.register_service("battery", lambda: None)
        daemon.start_all(enabled=["battery"])
        daemon.stop_all()

        status = daemon.service_status()
        self.assertIn("ব্যাটারি", status)
        self.assertIn("Telegram", status)
        self.assertFalse(status["ব্যাটারি"])

    def test_status_reports_restarts_and_uptime(self):
        daemon, _ = make_daemon()
        daemon.register_service("test", lambda: daemon.services["test"].stop.wait(1))
        daemon.start(enabled=["test"])
        status = daemon.status()
        daemon.shutdown()

        self.assertTrue(status["ok"])
        self.assertTrue(status["wake_lock"])
        self.assertIn("test", status["services"])
        self.assertIn("uptime_seconds", status)


# --------------------------------------------------------------------------- #
# Service ticks
# --------------------------------------------------------------------------- #

class TestTicks(unittest.TestCase):
    def test_battery_tick_pushes_alert_only_when_one_fires(self):
        daemon, fakes = make_daemon(control=FakeControl(alert="ব্যাটারি মাত্র ১৫ শতাংশ।"))
        result = daemon.battery_tick()
        self.assertEqual(fakes["control"].calls, 1)
        self.assertTrue(result["ok"])
        self.assertEqual(len(fakes["telegram"].pushes), 1)
        self.assertIn("১৫", fakes["telegram"].pushes[0])

        quiet, quiet_fakes = make_daemon(control=FakeControl(alert=None))
        quiet.battery_tick()
        self.assertEqual(quiet_fakes["telegram"].pushes, [])

    def test_reception_tick_skips_when_disabled(self):
        daemon, fakes = make_daemon()
        result = daemon.reception_tick()
        self.assertEqual(result["skipped"], "auto_reception off")
        self.assertEqual(fakes["receptionist"].polls, 0)

    def test_reception_tick_pushes_each_answered_call_once(self):
        answered = {
            "ok": True,
            "answered": True,
            "caller": "রহিম",
            "timestamp": "2026-11-06T10:00:00",
            "summary_bn": "রহিম জরুরি কাজে কল করেছিলেন।",
        }
        daemon, fakes = make_daemon(receptionist=FakeReceptionist(answered=answered))
        fakes["receptionist"].auto_reception = True

        daemon.reception_tick()
        daemon.reception_tick()
        self.assertEqual(len(fakes["telegram"].call_summaries), 1)

    def test_notifications_tick_sends_digest(self):
        messages = [
            {"app_bn": "হোয়াটসঅ্যাপ", "sender": "রহিম", "text": "কাল দেখা হবে?"},
            {"app_bn": "টেলিগ্রাম", "sender": "করিম", "text": "ফাইল পাঠিয়েছি"},
        ]
        daemon, fakes = make_daemon(responder=FakeResponder(messages=messages, replies=[{"ok": True}]))
        result = daemon.notifications_tick()

        self.assertEqual(result["count"], 2)
        push = fakes["telegram"].pushes[-1]
        self.assertIn("হোয়াটসঅ্যাপ", push)
        self.assertIn("রহিম", push)
        self.assertIn("অটো-রিপ্লাই", push)

    def test_notifications_tick_silent_without_new_messages(self):
        daemon, fakes = make_daemon(responder=FakeResponder())
        daemon.notifications_tick()
        self.assertEqual(fakes["telegram"].pushes, [])

    def test_daily_tick_runs_once_per_day(self):
        daemon, fakes = make_daemon()
        first = daemon.daily_tick()
        second = daemon.daily_tick()

        self.assertTrue(first["ok"])
        self.assertIn("সুপ্রভাত", first["text"])
        self.assertIn("শেখা", first["text"])
        self.assertIn("নিফটি", first["text"])
        self.assertEqual(fakes["memory"].upgrades, 1)
        self.assertEqual(fakes["briefing"].calls, 1)
        self.assertEqual(second["skipped"], "already ran today")
        self.assertTrue(fakes["voice"].spoken)

    def test_daily_tick_force_reruns(self):
        daemon, fakes = make_daemon()
        daemon.daily_tick()
        daemon.daily_tick(force=True)
        self.assertEqual(fakes["memory"].upgrades, 2)

    def test_push_without_telegram_configuration_is_safe(self):
        daemon, fakes = make_daemon()
        fakes["telegram"].token = ""
        result = daemon._push("হ্যালো")
        self.assertFalse(result["ok"])


# --------------------------------------------------------------------------- #
# Interactive CLI
# --------------------------------------------------------------------------- #

class TestInteractiveLoop(unittest.TestCase):
    def test_commands_run_until_exit(self):
        dispatcher = FakeDispatcher()
        lines = iter(["টর্চ জালাও", "ব্যাটারি কেমন?", "exit"])
        printed = []

        handled = interactive_loop(dispatcher, reader=lambda prompt: next(lines), writer=printed.append)

        self.assertEqual(handled, 2)
        self.assertEqual(dispatcher.texts, ["টর্চ জালাও", "ব্যাটারি কেমন?"])
        self.assertIn("বস, টর্চ জ্বালালাম।", printed)
        self.assertIn("বিদায়", printed[-1])

    def test_eof_stops_cleanly(self):
        def reader(prompt):
            raise EOFError

        handled = interactive_loop(FakeDispatcher(), reader=reader, writer=lambda line: None)
        self.assertEqual(handled, 0)

    def test_stop_event_prevents_reading(self):
        stop = threading.Event()
        stop.set()
        handled = interactive_loop(FakeDispatcher(), reader=lambda p: "x", writer=lambda l: None, stop_event=stop)
        self.assertEqual(handled, 0)

    def test_dispatch_crash_is_reported_and_loop_continues(self):
        class Broken:
            def __init__(self):
                self.calls = 0

            def process(self, text):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("kaboom")
                return {"ok": True, "reply_bn": "বস, ঠিক আছে।"}

        lines = iter(["প্রথম", "দ্বিতীয়", "exit"])
        printed = []
        handled = interactive_loop(Broken(), reader=lambda p: next(lines), writer=printed.append)

        self.assertEqual(handled, 2)
        self.assertTrue(any("সমস্যা" in line for line in printed))
        self.assertIn("বস, ঠিক আছে।", printed)


# --------------------------------------------------------------------------- #
# Doctor
# --------------------------------------------------------------------------- #

class TestDoctor(unittest.TestCase):
    def test_ready_environment(self):
        settings = FakeSettings(
            gemini_api_key="g", groq_api_key="gr",
            telegram_bot_token="t", admin_chat_id=42,
        )
        settings.env_path = Path(__file__)  # pretend .env exists
        memory = FakeMemory()
        report = doctor_report(
            settings=settings,
            memory=memory,
            runner=FakeRunner(devices=True),
            which=lambda name: f"/data/data/com.termux/files/usr/bin/{name}",
        )

        self.assertTrue(report["ok"])
        self.assertIn("প্রস্তুত", report["summary"])
        labels = [row["label"] for row in report["rows"]]
        self.assertIn("ADB", labels)
        self.assertIn("GEMINI_API_KEY", labels)

    def test_missing_everything_is_not_ready(self):
        settings = FakeSettings()
        report = doctor_report(
            settings=settings,
            memory=FakeMemory(),
            runner=FakeRunner(code=127),
            which=lambda name: None,
        )
        self.assertFalse(report["ok"])
        warnings = [row for row in report["rows"] if row["status"] == "warn"]
        self.assertTrue(warnings)
        self.assertIn("ঠিক করুন", report["summary"])

    def test_memory_failure_is_a_fail_row(self):
        class BrokenMemory:
            def stats(self):
                raise RuntimeError("db locked")

        report = doctor_report(
            settings=FakeSettings(gemini_api_key="g"),
            memory=BrokenMemory(),
            runner=FakeRunner(),
            which=lambda name: None,
        )
        self.assertFalse(report["ok"])
        row = next(item for item in report["rows"] if item["label"] == "SQLite মেমোরি")
        self.assertEqual(row["status"], "fail")

    def test_print_doctor_renders_table(self):
        report = doctor_report(settings=FakeSettings(), memory=FakeMemory(), runner=FakeRunner(), which=lambda n: None)
        lines = []
        print_doctor(report, out=lines.append)
        self.assertIn("প্রস্তুতি রিপোর্ট", lines[0])
        self.assertTrue(any("SQLite" in line for line in lines))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

class FakeDaemon:
    def __init__(self, result=None):
        self.dispatcher = FakeDispatcher(result)
        self.voice = FakeVoice()
        self.briefing = FakeBriefing()
        self.started = 0
        self.shutdowns = 0

    def start(self, enabled=None):
        self.started += 1
        return {"ok": True}

    def shutdown(self, timeout=5.0):
        self.shutdowns += 1
        return {"ok": True}


class TestCLI(unittest.TestCase):
    def run_main(self, argv, daemon=None, stop=None):
        lines = []
        code = main(argv, daemon=daemon or FakeDaemon(), out=lines.append, err=lines.append,
                    stop_event=stop)
        return code, lines

    def test_parser_has_all_commands(self):
        parser = build_parser()
        for command in ("daemon", "once", "voice", "briefing", "doctor"):
            self.assertIn(command, parser.format_help())

    def test_once_prints_bengali_reply(self):
        daemon = FakeDaemon()
        code, lines = self.run_main(["once", "টর্চ", "জালাও"], daemon)
        self.assertEqual(code, 0)
        self.assertEqual(daemon.dispatcher.texts, ["টর্চ জালাও"])
        self.assertIn("বস, টর্চ জ্বালালাম।", lines)

    def test_once_json_output(self):
        daemon = FakeDaemon()
        code, lines = self.run_main(["once", "টর্চ", "--json"], daemon)
        self.assertEqual(code, 0)
        payload = json.loads(lines[-1])
        self.assertEqual(payload["reply_bn"], "বস, টর্চ জ্বালালাম।")

    def test_once_failure_exit_code(self):
        daemon = FakeDaemon(result={"ok": False, "reply_bn": "বস, পারলাম না।", "error": "no adb"})
        code, _ = self.run_main(["once", "কল"], daemon)
        self.assertEqual(code, 1)

    def test_briefing_prints_and_speaks(self):
        daemon = FakeDaemon()
        code, lines = self.run_main(["briefing"], daemon)
        self.assertEqual(code, 0)
        self.assertIn("নিফটি", lines[0])
        self.assertTrue(daemon.voice.spoken)

    def test_voice_command(self):
        daemon = FakeDaemon()
        code, lines = self.run_main(["voice", "--seconds", "3"], daemon)
        self.assertEqual(code, 0)
        self.assertEqual(daemon.dispatcher.texts, ["টর্চ জালাও"])
        self.assertTrue(any("শুনলাম" in line for line in lines))
        self.assertTrue(daemon.voice.spoken)

    def test_voice_command_with_no_speech(self):
        daemon = FakeDaemon()
        daemon.voice.transcript = ""
        code, lines = self.run_main(["voice"], daemon)
        self.assertEqual(code, 1)
        self.assertEqual(daemon.dispatcher.texts, [])

    def test_daemon_no_cli_starts_and_shuts_down(self):
        stop = threading.Event()
        stop.set()
        daemon = FakeDaemon()
        code, lines = self.run_main(["daemon", "--no-cli"], daemon, stop=stop)
        self.assertEqual(code, 0)
        self.assertEqual(daemon.started, 1)
        self.assertEqual(daemon.shutdowns, 1)
        self.assertIn("ব্যাকগ্রাউন্ডে", lines[0])

    def test_default_command_is_daemon(self):
        stop = threading.Event()
        stop.set()
        daemon = FakeDaemon()
        code, _ = self.run_main(["daemon", "--no-cli"], daemon, stop=stop)
        self.assertEqual(daemon.started, 1)
        self.assertEqual(code, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
