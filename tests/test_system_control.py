"""
tests/test_system_control.py — unit tests for ORBIT's device layer.

No Termux binaries are needed: every command goes through an injected fake
runner (or a patched ``subprocess.run``), so this runs on desktop/CI.

    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import subprocess
import time
import unittest
from unittest import mock

import system_control as sc
from system_control import (
    CommandResult,
    SystemControl,
    normalise_number,
    run_command,
    to_ascii_digits,
    to_bengali_digits,
    transliterate,
)


class FakeRunner:
    """Queue of CommandResults; records every command it was given."""

    def __init__(self, *results: CommandResult | list) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, cmd, timeout=None):
        self.calls.append(list(cmd))
        if not self.results:
            return CommandResult(code=1, stderr="no queued result")
        result = self.results.pop(0)
        if isinstance(result, CommandResult):
            return result
        if isinstance(result, str):  # shorthand: literal stdout, exit 0
            return CommandResult(code=0, stdout=result)
        return CommandResult(code=0, stdout=json.dumps(result))  # any JSON payload


def ok(stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(code=0, stdout=stdout, stderr=stderr)


def err(message: str = "boom", code: int = 1) -> CommandResult:
    return CommandResult(code=code, stderr=message)


def control(*results, voice=None, ttl: float = 300.0) -> SystemControl:
    return SystemControl(runner=FakeRunner(*results), voice=voice, contact_ttl=ttl)


class FakeVoice:
    def __init__(self, result: bool = True) -> None:
        self.spoken: list[str] = []
        self.result = result
        self.last_error = None

    def speak(self, text: str, **_kwargs) -> bool:
        self.spoken.append(text)
        return self.result


BATTERY_PAYLOAD = {
    "health": "GOOD",
    "percentage": 17,
    "plugged": "UNPLUGGED",
    "status": "DISCHARGING",
    "temperature": 31.5,
    "current": -450,
}

CONTACTS = [
    {"name": "Rahim Uddin", "number": "+8801712345678"},
    {"name": "রহিমা বেগম", "number": "01898765432"},
    {"name": "Karim Sheikh", "number": "01911122233"},
    {"name": "Battery Service", "number": "16263"},
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

class TestHelpers(unittest.TestCase):
    def test_digit_conversion(self):
        self.assertEqual(to_bengali_digits(87), "৮৭")
        self.assertEqual(to_ascii_digits("০১৭১২"), "01712")
        self.assertEqual(to_bengali_digits(31.5), "৩১.৫")

    def test_normalise_number(self):
        self.assertEqual(normalise_number("+880 171-234-5678"), "+8801712345678")
        self.assertEqual(normalise_number("০১৭১২৩৪৫৬৭৮"), "01712345678")
        self.assertEqual(normalise_number(None), "")
        self.assertEqual(normalise_number("abc"), "")

    def test_transliterate_matches_banglish(self):
        # Bengali's inherent vowel is lost by a naive map: রহিম -> "rhim".
        # The consonant skeleton is what makes "rahim" match anyway.
        self.assertEqual(transliterate("রহিম"), "rhim")
        self.assertEqual(sc._name_skeleton("রহিম"), sc._name_skeleton("rahim"))
        self.assertEqual(sc._normalise_name("Rahim Uddin"), "rahimuddin")

    def test_extract_json_tolerates_warnings(self):
        self.assertEqual(sc._extract_json('warning: x\n[{"a": 1}]'), [{"a": 1}])
        self.assertEqual(sc._extract_json('{"a": 1}'), {"a": 1})
        with self.assertRaises(ValueError):
            sc._extract_json("no json here")

    def test_run_command_missing_binary(self):
        with mock.patch.object(sc, "which_binary", return_value=None):
            result = run_command(["termux-torch", "on"])
        self.assertFalse(result.ok)
        self.assertIn("pkg install termux-api", result.error)

    def test_run_command_success_and_failure(self):
        completed = subprocess.CompletedProcess(["x"], 0, stdout="fine\n", stderr="")
        with mock.patch.object(sc, "which_binary", return_value="/usr/bin/x"), \
                mock.patch.object(sc.subprocess, "run", return_value=completed) as run:
            result = run_command(["termux-battery-status"], timeout=5)
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "fine")
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

        with mock.patch.object(sc, "which_binary", return_value="/usr/bin/x"), \
                mock.patch.object(sc.subprocess, "run", return_value=subprocess.CompletedProcess([], 3, "", "nope")):
            result = run_command(["termux-torch", "on"])
        self.assertFalse(result.ok)
        self.assertEqual(result.code, 3)

    def test_run_command_timeout(self):
        with mock.patch.object(sc, "which_binary", return_value="/usr/bin/x"), \
                mock.patch.object(sc.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 5)):
            result = run_command(["termux-location"], timeout=5)
        self.assertEqual(result.code, 124)
        self.assertIn("timed out", result.error)

    def test_run_command_file_not_found(self):
        with mock.patch.object(sc, "which_binary", return_value="/usr/bin/x"), \
                mock.patch.object(sc.subprocess, "run", side_effect=FileNotFoundError()):
            result = run_command(["termux-torch", "on"])
        self.assertEqual(result.code, 127)


# --------------------------------------------------------------------------- #
# Torch
# --------------------------------------------------------------------------- #

class TestTorch(unittest.TestCase):
    def test_on_off(self):
        fake = FakeRunner(ok(), ok())
        c = SystemControl(runner=fake)
        self.assertEqual(c.torch("on")["state"], "on")
        self.assertEqual(c.torch("off")["state"], "off")
        self.assertEqual(fake.calls, [["termux-torch", "on"], ["termux-torch", "off"]])

    def test_bengali_and_boolean_values(self):
        fake = FakeRunner(ok(), ok(), ok())
        c = SystemControl(runner=fake)
        self.assertTrue(c.torch("চালু")["ok"])
        self.assertTrue(c.torch(True)["ok"])
        self.assertTrue(c.torch(False)["ok"])
        self.assertEqual([call[1] for call in fake.calls], ["on", "on", "off"])

    def test_invalid_state_never_runs_command(self):
        fake = FakeRunner()
        c = SystemControl(runner=fake)
        result = c.torch("maybe")
        self.assertFalse(result["ok"])
        self.assertIn("invalid torch state", result["error"])
        self.assertEqual(fake.calls, [])

    def test_missing_binary(self):
        c = SystemControl(runner=FakeRunner(CommandResult(code=127, error="termux-torch not found (pkg install termux-api)")))
        result = c.torch("on")
        self.assertFalse(result["ok"])
        self.assertIn("termux-api", result["error"])
        self.assertEqual(c.last_error, result["error"])


# --------------------------------------------------------------------------- #
# Battery
# --------------------------------------------------------------------------- #

class TestBattery(unittest.TestCase):
    def test_parses_status(self):
        c = control(json.dumps(BATTERY_PAYLOAD))
        result = c.battery_status()
        self.assertTrue(result["ok"])
        self.assertEqual(result["percentage"], 17)
        self.assertEqual(result["status"], "DISCHARGING")
        self.assertEqual(result["plugged"], "UNPLUGGED")
        self.assertEqual(result["temperature"], 31.5)
        self.assertTrue(result["low"])
        self.assertFalse(result["full"])
        self.assertIn("১৭ শতাংশ", result["message_bn"])
        self.assertIn("চার্জ কমছে", result["message_bn"])
        self.assertIn("৩১.৫", result["message_bn"])
        json.dumps(result)  # must stay serialisable for Telegram

    def test_charging_full(self):
        payload = {**BATTERY_PAYLOAD, "percentage": 100, "status": "FULL", "plugged": "PLUGGED_AC"}
        result = control(json.dumps(payload)).battery_status()
        self.assertTrue(result["full"])
        self.assertIn("পুরো চার্জ", result["message_bn"])
        self.assertIn("চার্জারে লাগানো", result["message_bn"])

    def test_unreadable_and_missing_percentage(self):
        self.assertFalse(control("not json").battery_status()["ok"])
        self.assertFalse(control("{").battery_status()["ok"])
        self.assertFalse(control(json.dumps({"percentage": "x"})).battery_status()["ok"])
        self.assertFalse(control(json.dumps({"status": "CHARGING"})).battery_status()["ok"])

    def test_command_failure(self):
        result = control(err("termux-api missing")).battery_status()
        self.assertFalse(result["ok"])
        self.assertIn("termux-api missing", result["error"])

    def test_alert_boundaries(self):
        self.assertIsNone(SystemControl.battery_alert_message(21, "DISCHARGING"))
        self.assertIsNone(SystemControl.battery_alert_message(20, "DISCHARGING"))
        low = SystemControl.battery_alert_message(19, "DISCHARGING")
        self.assertEqual(low[0], "low")
        self.assertIn("১৯", low[1])
        full = SystemControl.battery_alert_message(100, "CHARGING")
        self.assertEqual(full[0], "full")
        self.assertEqual(SystemControl.battery_alert_message(99, "FULL")[0], "full")
        self.assertIsNone(SystemControl.battery_alert_message(80, "CHARGING"))

    def test_check_battery_speaks_low_alert(self):
        voice = FakeVoice()
        c = control(json.dumps(BATTERY_PAYLOAD), voice=voice)
        result = c.check_battery()
        self.assertEqual(result["alert"], "low")
        self.assertTrue(result["spoken"])
        self.assertEqual(len(voice.spoken), 1)
        self.assertIn("চার্জে লাগান", voice.spoken[0])

    def test_check_battery_cooldown_and_force(self):
        voice = FakeVoice()
        c = control(json.dumps(BATTERY_PAYLOAD), json.dumps(BATTERY_PAYLOAD), json.dumps(BATTERY_PAYLOAD), voice=voice)
        c.check_battery()
        c.check_battery()                      # suppressed by cooldown
        self.assertEqual(len(voice.spoken), 1)
        c.check_battery(force=True)            # explicit repeat allowed
        self.assertEqual(len(voice.spoken), 2)

    def test_check_battery_notify_false(self):
        voice = FakeVoice()
        c = control(json.dumps(BATTERY_PAYLOAD), voice=voice)
        result = c.check_battery(notify=False)
        self.assertEqual(result["alert"], "low")
        self.assertFalse(result["spoken"])
        self.assertEqual(voice.spoken, [])

    def test_alert_state_resets_when_battery_recovers(self):
        healthy = json.dumps({**BATTERY_PAYLOAD, "percentage": 80, "status": "CHARGING"})
        c = control(json.dumps(BATTERY_PAYLOAD), healthy, json.dumps(BATTERY_PAYLOAD), voice=FakeVoice())
        c.check_battery()
        self.assertIn("low", c._last_alert)
        c.check_battery()
        self.assertEqual(c._last_alert, {})   # cleared -> a future drop alerts again

    def test_full_battery_alert(self):
        voice = FakeVoice()
        payload = json.dumps({**BATTERY_PAYLOAD, "percentage": 100, "status": "FULL"})
        result = control(payload, voice=voice).check_battery()
        self.assertEqual(result["alert"], "full")
        self.assertIn("চার্জার খুলে ফেলুন", voice.spoken[0])

    def test_battery_failure_returns_error(self):
        result = control(err("no battery api")).check_battery()
        self.assertFalse(result["ok"])

    def test_voice_failure_does_not_raise(self):
        result = control(json.dumps(BATTERY_PAYLOAD), voice=FakeVoice(result=False)).check_battery()
        self.assertTrue(result["ok"])
        self.assertFalse(result["spoken"])

    def test_monitor_thread_starts_and_stops(self):
        voice = FakeVoice()
        c = control(json.dumps(BATTERY_PAYLOAD), json.dumps({**BATTERY_PAYLOAD, "percentage": 85, "status": "CHARGING"}), voice=voice)
        thread = c.start_battery_monitor(interval=60)
        self.assertTrue(thread.daemon)
        self.assertTrue(thread.is_alive())
        self.assertTrue(c.monitor_running)
        c.stop_battery_monitor()
        self.assertFalse(c.monitor_running)
        self.assertGreaterEqual(len(voice.spoken), 1)   # low alert spoke on first pass


# --------------------------------------------------------------------------- #
# Volume
# --------------------------------------------------------------------------- #

VOLUMES = [
    {"stream": "music", "volume": 8, "max_volume": 15},
    {"stream": "call", "volume": 3, "max_volume": 7},
]


class TestVolume(unittest.TestCase):
    def test_sets_mapped_level(self):
        fake = FakeRunner(VOLUMES, ok())
        c = SystemControl(runner=fake)
        result = c.set_volume(50)
        self.assertTrue(result["ok"])
        self.assertEqual(result["level"], 50)
        self.assertEqual(result["raw"], 8)          # round(0.5 * 15) == 8
        self.assertEqual(fake.calls[1], ["termux-volume", "music", "8"])
        self.assertIn("ভলিউম ৫০", result["message_bn"])

    def test_zero_and_hundred(self):
        fake = FakeRunner(VOLUMES, ok(), VOLUMES, ok())
        c = SystemControl(runner=fake)
        self.assertEqual(c.set_volume(0)["raw"], 0)
        self.assertEqual(c.set_volume(100)["raw"], 15)

    def test_clamps_out_of_range(self):
        fake = FakeRunner(VOLUMES, ok(), VOLUMES, ok())
        c = SystemControl(runner=fake)
        self.assertEqual(c.set_volume(150)["level"], 100)
        self.assertEqual(c.set_volume("১৫")["level"], 15)

    def test_direction_mapping(self):
        fake = FakeRunner(VOLUMES, ok(), VOLUMES, ok(), VOLUMES, ok(), VOLUMES, ok())
        c = SystemControl(runner=fake)
        self.assertEqual(c.set_volume(direction="up")["level"], 63)
        self.assertEqual(c.set_volume(direction="down")["level"], 43)
        self.assertEqual(c.set_volume(direction="mute")["raw"], 0)
        self.assertEqual(c.set_volume(direction="unmute")["level"], 50)

    def test_other_stream(self):
        fake = FakeRunner(VOLUMES, ok())
        c = SystemControl(runner=fake)
        result = c.set_volume(100, stream="call")
        self.assertEqual(result["raw"], 7)
        self.assertEqual(fake.calls[1], ["termux-volume", "call", "7"])

    def test_dict_shaped_payload(self):
        payload = {"music": {"volume": 4, "max_volume": 10}}
        fake = FakeRunner(payload, ok())
        c = SystemControl(runner=fake)
        self.assertEqual(c.set_volume(50)["raw"], 5)

    def test_validation_errors(self):
        c = SystemControl(runner=FakeRunner(VOLUMES, VOLUMES, VOLUMES))
        self.assertIn("unknown audio stream", c.set_volume(50, stream="alarm")["error"])
        self.assertIn("invalid volume level", SystemControl(runner=FakeRunner(VOLUMES)).set_volume("loud")["error"])
        self.assertIn("invalid volume direction", SystemControl(runner=FakeRunner(VOLUMES)).set_volume(direction="sideways")["error"])
        self.assertIn("needs a level", SystemControl(runner=FakeRunner(VOLUMES)).set_volume()["error"])

    def test_read_and_write_failures(self):
        self.assertFalse(SystemControl(runner=FakeRunner(err("no termux-api"))).set_volume(50)["ok"])
        self.assertFalse(SystemControl(runner=FakeRunner(VOLUMES, err("denied"))).set_volume(50)["ok"])
        self.assertFalse(SystemControl(runner=FakeRunner("[]")).set_volume(50)["ok"])


# --------------------------------------------------------------------------- #
# Wi-Fi
# --------------------------------------------------------------------------- #

class TestWifi(unittest.TestCase):
    def test_on_off(self):
        fake = FakeRunner(ok(), ok())
        c = SystemControl(runner=fake)
        self.assertTrue(c.toggle_wifi("on")["ok"])
        self.assertTrue(c.toggle_wifi("off")["ok"])
        self.assertEqual(fake.calls, [["termux-wifi-enable", "true"], ["termux-wifi-enable", "false"]])

    def test_aliases(self):
        fake = FakeRunner(ok(), ok())
        c = SystemControl(runner=fake)
        c.toggle_wifi("চালু")
        c.toggle_wifi(False)
        self.assertEqual([call[1] for call in fake.calls], ["true", "false"])

    def test_invalid_and_failure(self):
        fake = FakeRunner(err("requires root on Android 10+"))
        c = SystemControl(runner=fake)
        self.assertFalse(c.toggle_wifi("sideways")["ok"])
        self.assertEqual(fake.calls, [])
        result = c.toggle_wifi("on")
        self.assertFalse(result["ok"])
        self.assertIn("requires root", result["error"])


# --------------------------------------------------------------------------- #
# Contacts
# --------------------------------------------------------------------------- #

class TestContacts(unittest.TestCase):
    def test_fuzzy_bengali_name_from_banglish(self):
        c = control(CONTACTS)
        result = c.find_contact("rahim")
        self.assertTrue(result["ok"])
        self.assertIn(result["name"], ("Rahim Uddin", "রহিমা বেগম"))
        self.assertTrue(result["number"])

    def test_bengali_script_query(self):
        result = control(CONTACTS).find_contact("রহিমা")
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "রহিমা বেগম")
        self.assertEqual(result["number"], "01898765432")

    def test_exact_and_substring(self):
        c = control(CONTACTS)
        self.assertEqual(c.find_contact("Karim Sheikh")["name"], "Karim Sheikh")
        self.assertTrue(c.find_contact("karim")["ok"])

    def test_number_match_ignores_country_code(self):
        c = control(CONTACTS)
        self.assertEqual(c.find_contact("01712345678")["name"], "Rahim Uddin")
        self.assertEqual(c.find_contact("+880 171 234 5678")["name"], "Rahim Uddin")
        self.assertEqual(c.find_contact("০১৮৯৮৭৬৫৪৩২")["name"], "রহিমা বেগম")

    def test_unknown_number_and_name(self):
        c = control(CONTACTS, CONTACTS)
        self.assertIn("no contact with number", c.find_contact("01700000000")["error"])
        self.assertIn("not found", c.find_contact("Zulfikar Ali Bhutto")["error"])

    def test_empty_query(self):
        self.assertFalse(control(CONTACTS).find_contact("   ")["ok"])

    def test_cache_avoids_second_lookup(self):
        fake = FakeRunner(CONTACTS)
        c = SystemControl(runner=fake)
        c.find_contact("rahim")
        c.find_contact("karim")
        self.assertEqual(len(fake.calls), 1)

    def test_cache_expiry(self):
        fake = FakeRunner(CONTACTS, CONTACTS)
        c = SystemControl(runner=fake, contact_ttl=0.0)
        c.find_contact("rahim")
        c.find_contact("rahim")
        self.assertEqual(len(fake.calls), 2)

    def test_missing_or_broken_contact_list(self):
        self.assertFalse(control(CommandResult(code=127, error="termux-contact-list not found")).find_contact("x")["ok"])
        self.assertFalse(control("not json").find_contact("x")["ok"])
        self.assertFalse(control("[]").find_contact("x")["ok"])

    def test_resolve_contact(self):
        c = control(CONTACTS)
        spoken = c.resolve_contact(contact="Rahim", number="০১৯৯৯৯৯৯৯৯৯")
        self.assertEqual(spoken["number"], "01999999999")
        self.assertEqual(spoken["source"], "spoken")

        resolved = c.resolve_contact(contact="Karim")
        self.assertEqual(resolved["number"], "01911122233")
        self.assertEqual(resolved["source"], "contacts")

        self.assertFalse(c.resolve_contact()["ok"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
