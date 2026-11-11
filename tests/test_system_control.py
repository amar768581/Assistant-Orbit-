"""
tests/test_system_control.py — battery, health, volume, Wi-Fi and contacts on a
PC. No real hardware: psutil is faked, media keys are recorded, contacts come
from a temporary JSON file.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
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
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, cmd, timeout=None):
        self.calls.append(list(cmd))
        if not self.results:
            return CommandResult(code=1, stderr="no queued result")
        result = self.results.pop(0)
        if isinstance(result, CommandResult):
            return result
        return CommandResult(code=0, stdout=str(result))


class FakeBattery:
    def __init__(self, percent=17, plugged=False):
        self.percent = percent
        self.power_plugged = plugged
        self.secsleft = -1


class FakePsutil:
    def __init__(self, battery="default"):
        self.battery = FakeBattery() if battery == "default" else battery

    def sensors_battery(self):
        return self.battery

    def cpu_percent(self, interval=None):
        return 12.5

    def cpu_count(self):
        return 8

    def virtual_memory(self):
        return SimpleNamespace(
            total=32 * 1024**3, used=10 * 1024**3, available=22 * 1024**3, percent=31.2
        )

    def disk_usage(self, anchor):
        return SimpleNamespace(
            total=512 * 1024**3, used=300 * 1024**3, free=212 * 1024**3, percent=58.6
        )

    def boot_time(self):
        return time.time() - 3600

    def process_iter(self, fields):
        return []


class FakeVoice:
    def __init__(self, result=True):
        self.spoken = []
        self.result = result
        self.last_error = None

    def speak(self, text, **_kwargs):
        self.spoken.append(text)
        return self.result


def control(*results, voice=None, ttl=300.0, battery="default", psutil_module=None):
    return SystemControl(
        runner=FakeRunner(*results),
        voice=voice,
        contact_ttl=ttl,
        psutil_module=psutil_module or FakePsutil(battery=battery),
        platform_name="Windows",
    )


CONTACTS = [
    {"name": "Rahim Uddin", "number": "+8801712345678"},
    {"name": "রহিমা বেগম", "number": "01898765432"},
    {"name": "Karim Sheikh", "number": "01911122233"},
]


def write_contacts(directory: str) -> Path:
    path = Path(directory) / "contacts.json"
    path.write_text(json.dumps(CONTACTS, ensure_ascii=False), encoding="utf-8")
    return path


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
            result = run_command(["definitely-not-a-real-binary"])
        self.assertFalse(result.ok)
        self.assertIn("not found", result.error)

    def test_run_command_success_and_failure(self):
        completed = subprocess.CompletedProcess(["x"], 0, stdout="fine\n", stderr="")
        with mock.patch.object(sc, "which_binary", return_value="/usr/bin/x"), \
                mock.patch.object(sc.subprocess, "run", return_value=completed) as run:
            result = run_command(["x"], timeout=5)
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "fine")
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

        with mock.patch.object(sc, "which_binary", return_value="/usr/bin/x"), \
                mock.patch.object(sc.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 3, "", "nope")):
            result = run_command(["x"])
        self.assertFalse(result.ok)
        self.assertEqual(result.code, 3)

    def test_run_command_timeout_and_file_not_found(self):
        with mock.patch.object(sc, "which_binary", return_value="/usr/bin/x"), \
                mock.patch.object(sc.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 5)):
            result = run_command(["x"], timeout=5)
        self.assertEqual(result.code, 124)
        self.assertIn("timed out", result.error)

        with mock.patch.object(sc, "which_binary", return_value="/usr/bin/x"), \
                mock.patch.object(sc.subprocess, "run", side_effect=FileNotFoundError()):
            result = run_command(["x"])
        self.assertEqual(result.code, 127)


class TestBattery(unittest.TestCase):
    def test_parses_psutil_battery(self):
        result = control().battery_status()
        self.assertTrue(result["ok"])
        self.assertEqual(result["percentage"], 17)
        self.assertEqual(result["status"], "DISCHARGING")
        self.assertEqual(result["plugged"], "UNPLUGGED")
        self.assertIsNone(result["temperature"])
        self.assertTrue(result["low"])
        self.assertFalse(result["full"])
        self.assertIn("১৭ শতাংশ", result["message_bn"])
        self.assertIn("চার্জ কমছে", result["message_bn"])
        json.dumps(result)

    def test_charging_full(self):
        result = control(battery=FakeBattery(100, True)).battery_status()
        self.assertTrue(result["full"])
        self.assertEqual(result["status"], "FULL")
        self.assertIn("পুরো চার্জ", result["message_bn"])
        self.assertIn("চার্জারে লাগানো", result["message_bn"])

    def test_desktop_without_battery_is_clean_failure(self):
        result = control(battery=None).battery_status()
        self.assertFalse(result["ok"])
        self.assertIn("ব্যাটারি নেই", result["message_bn"])

    def test_psutil_missing(self):
        c = SystemControl(psutil_module=None, platform_name="Windows")
        c._psutil = None
        with mock.patch.dict("sys.modules", {"psutil": None}):
            result = c.battery_status()
        self.assertFalse(result["ok"])
        self.assertIn("psutil", result["error"])

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
        result = control(voice=voice).check_battery()
        self.assertEqual(result["alert"], "low")
        self.assertTrue(result["spoken"])
        self.assertEqual(len(voice.spoken), 1)
        self.assertIn("চার্জে লাগান", voice.spoken[0])

    def test_check_battery_cooldown_and_force(self):
        voice = FakeVoice()
        c = control(voice=voice)
        c.check_battery()
        c.check_battery()
        self.assertEqual(len(voice.spoken), 1)
        c.check_battery(force=True)
        self.assertEqual(len(voice.spoken), 2)

    def test_check_battery_notify_false(self):
        voice = FakeVoice()
        result = control(voice=voice).check_battery(notify=False)
        self.assertEqual(result["alert"], "low")
        self.assertFalse(result["spoken"])
        self.assertEqual(voice.spoken, [])

    def test_alert_state_resets_when_battery_recovers(self):
        c = SystemControl(
            psutil_module=FakePsutil(FakeBattery(17, False)),
            voice=FakeVoice(),
            platform_name="Windows",
        )
        c.check_battery()
        self.assertIn("low", c._last_alert)
        c._psutil = FakePsutil(FakeBattery(85, True))
        c.check_battery()
        self.assertEqual(c._last_alert, {})

    def test_monitor_thread_starts_and_stops(self):
        voice = FakeVoice()
        c = control(voice=voice)
        thread = c.start_battery_monitor(interval=60)
        self.assertTrue(thread.daemon)
        self.assertTrue(thread.is_alive())
        self.assertTrue(c.monitor_running)
        c.stop_battery_monitor()
        self.assertFalse(c.monitor_running)
        self.assertGreaterEqual(len(voice.spoken), 1)

    def test_voice_failure_does_not_raise(self):
        result = control(voice=FakeVoice(result=False)).check_battery()
        self.assertTrue(result["ok"])
        self.assertFalse(result["spoken"])


class TestTorchAndHealth(unittest.TestCase):
    def test_torch_is_reported_as_unsupported(self):
        result = control().torch("on")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "torch_not_supported")
        self.assertIn("টর্চ নেই", result["message_bn"])

    def test_health_uses_psutil(self):
        result = control().system_health()
        self.assertTrue(result["ok"])
        self.assertEqual(result["cpu_percent"], 12.5)
        self.assertIn("সিপিইউ ১২.৫", result["summary_bn"])


class TestVolume(unittest.TestCase):
    def test_absolute_level_uses_media_keys(self):
        keys = []
        c = SystemControl(key_sender=lambda vk: keys.append(vk) or True, platform_name="Windows")
        result = c.set_volume(60)
        self.assertTrue(result["ok"])
        self.assertEqual(result["level"], 60)
        self.assertEqual(result["raw"], 60)
        self.assertEqual(result["max_volume"], 100)
        self.assertIn("ভলিউম ৬০", result["message_bn"])
        self.assertTrue(keys)                 # 50 -> 60 is ~5 up presses
        self.assertEqual(set(keys), {sc.VK_VOLUME_UP})

    def test_direction_mapping(self):
        c = SystemControl(key_sender=lambda vk: True, platform_name="Windows")
        self.assertEqual(c.set_volume(direction="up")["level"], 60)
        self.assertEqual(c.set_volume(direction="down")["level"], 50)
        self.assertEqual(c.set_volume(direction="mute")["level"], 0)
        self.assertEqual(c.set_volume(direction="unmute")["level"], 30)

    def test_clamps_and_bengali_digits(self):
        c = SystemControl(key_sender=lambda vk: True, platform_name="Windows")
        self.assertEqual(c.set_volume(150)["level"], 100)
        self.assertEqual(c.set_volume("১৫")["level"], 15)

    def test_validation_errors(self):
        c = SystemControl(key_sender=lambda vk: True, platform_name="Windows")
        self.assertIn("unknown audio stream", c.set_volume(50, stream="alarm")["error"])
        self.assertIn("invalid volume level", c.set_volume("loud")["error"])
        self.assertIn("invalid volume direction", c.set_volume(direction="sideways")["error"])
        self.assertIn("needs a level", c.set_volume()["error"])

    def test_linux_mixer(self):
        runner = FakeRunner(CommandResult(code=0))
        c = SystemControl(runner=runner, platform_name="Linux")
        result = c.set_volume(40)
        self.assertTrue(result["ok"])
        self.assertEqual(runner.calls[0][0], "pactl")

    def test_key_sender_failure_is_contained(self):
        def broken(vk):
            raise OSError("no keys")

        c = SystemControl(key_sender=broken, platform_name="Windows")
        result = c.set_volume(55)
        self.assertFalse(result["ok"])
        self.assertIn("volume", result["error"])


class TestWifi(unittest.TestCase):
    def test_status_connected(self):
        runner = FakeRunner("    State : connected\n    SSID : home")
        result = SystemControl(runner=runner, platform_name="Windows").wifi_status()
        self.assertTrue(result["ok"])
        self.assertTrue(result["connected"])

    def test_toggle_commands(self):
        runner = FakeRunner(CommandResult(0), CommandResult(0))
        c = SystemControl(runner=runner, platform_name="Windows")
        self.assertTrue(c.toggle_wifi("on")["ok"])
        self.assertTrue(c.toggle_wifi("off")["ok"])
        self.assertIn("admin=enabled", runner.calls[0])
        self.assertIn("admin=disabled", runner.calls[1])

    def test_invalid_and_denied(self):
        runner = FakeRunner(CommandResult(5, stderr="requires elevation"))
        c = SystemControl(runner=runner, platform_name="Windows")
        self.assertFalse(c.toggle_wifi("sideways")["ok"])
        self.assertEqual(runner.calls, [])
        result = c.toggle_wifi("on")
        self.assertFalse(result["ok"])
        self.assertIn("অ্যাডমিন", result["message_bn"])


class TestContacts(unittest.TestCase):
    def test_fuzzy_bengali_name_from_banglish(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_contacts(tmp)
            c = SystemControl(contacts_path=path, platform_name="Windows")
            result = c.find_contact("rahim")
        self.assertTrue(result["ok"])
        self.assertIn(result["name"], ("Rahim Uddin", "রহিমা বেগম"))
        self.assertTrue(result["number"])

    def test_bengali_script_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = SystemControl(contacts_path=write_contacts(tmp), platform_name="Windows").find_contact("রহিমা")
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "রহিমা বেগম")
        self.assertEqual(result["number"], "01898765432")

    def test_number_match_ignores_country_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = SystemControl(contacts_path=write_contacts(tmp), platform_name="Windows")
            self.assertEqual(c.find_contact("01712345678")["name"], "Rahim Uddin")
            self.assertEqual(c.find_contact("+880 171 234 5678")["name"], "Rahim Uddin")
            self.assertEqual(c.find_contact("০১৮৯৮৭৬৫৪৩২")["name"], "রহিমা বেগম")

    def test_unknown_number_and_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = SystemControl(contacts_path=write_contacts(tmp), platform_name="Windows")
            self.assertIn("no contact with number", c.find_contact("01700000000")["error"])
            self.assertIn("not found", c.find_contact("Zulfikar Ali Bhutto")["error"])

    def test_empty_query_and_missing_file(self):
        self.assertFalse(control().find_contact("   ")["ok"])
        with tempfile.TemporaryDirectory() as tmp:
            c = SystemControl(contacts_path=Path(tmp) / "missing.json", platform_name="Windows")
            self.assertFalse(c.find_contact("rahim")["ok"])

    def test_cache_and_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_contacts(tmp)
            c = SystemControl(contacts_path=path, platform_name="Windows")
            c.find_contact("rahim")
            c.find_contact("karim")
            self.assertEqual(len(c._contacts), 3)
            c.contact_ttl = 0.0
            path.write_text("[]", encoding="utf-8")
            self.assertEqual(c.list_contacts(), [])   # re-read after expiry

    def test_resolve_contact(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = SystemControl(contacts_path=write_contacts(tmp), platform_name="Windows")
            spoken = c.resolve_contact(contact="Rahim", number="০১৯৯৯৯৯৯৯৯৯")
            self.assertEqual(spoken["number"], "01999999999")
            self.assertEqual(spoken["source"], "spoken")

            resolved = c.resolve_contact(contact="Karim")
            self.assertEqual(resolved["number"], "01911122233")
            self.assertEqual(resolved["source"], "contacts")

            self.assertFalse(c.resolve_contact()["ok"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
