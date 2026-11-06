"""
tests/test_adb_automation.py — app launching, WhatsApp deep links and vision
screen reading. No adb binary, no phone and no network: the runner writes the
"screenshot" itself and the session is a fake Gemini endpoint.
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from adb_automation import AdbAutomation, normalise_international
from system_control import CommandResult, SystemControl

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096


class FakeRunner:
    """Simulates adb: `pull` writes the PNG where adb would."""

    def __init__(self, fail_contains=None, png=PNG_BYTES, pull_ok=True):
        self.calls = []
        self.fail_contains = fail_contains
        self.png = png
        self.pull_ok = pull_ok

    def __call__(self, cmd, timeout=None):
        self.calls.append(list(cmd))
        joined = " ".join(str(part) for part in cmd)
        needles = self.fail_contains
        if isinstance(needles, str):
            needles = (needles,)
        if needles and any(needle in joined for needle in needles):
            return CommandResult(code=1, stderr=f"failed: {joined}")
        if "pull" in cmd:
            if not self.pull_ok:
                return CommandResult(code=1, stderr="adb: device offline")
            Path(cmd[-1]).write_bytes(self.png)
            return CommandResult(code=0)
        return CommandResult(code=0)

    def count(self, *needles):
        return len([c for c in self.calls if all(n in " ".join(c) for n in needles)])


class FakeResponse:
    def __init__(self, status=200, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is not None:
            return self._payload
        raise ValueError("no json")


class FakeSession:
    def __init__(self, statuses=(200,), description="স্ক্রিনে হোয়াটসঅ্যাপ চ্যাট খোলা আছে, একটা নতুন মেসেজ এসেছে।"):
        self.statuses = list(statuses)
        self.description = description
        self.posts = []

    def post(self, url, params=None, json=None, timeout=None):
        self.posts.append({"url": url, "params": params, "json": json, "timeout": timeout})
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status != 200:
            return FakeResponse(status=status, text="server error")
        payload = {"candidates": [{"content": {"parts": [{"text": self.description}]}}]}
        return FakeResponse(status=200, payload=payload)


class FakeSettings:
    def __init__(self, gemini_key="test-key"):
        self.gemini_api_key = gemini_key


class ContactsRunner:
    """Only answers the address book; everything else succeeds."""

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, timeout=None):
        self.calls.append(list(cmd))
        if cmd[:1] == ["termux-contact-list"]:
            return CommandResult(code=0, stdout=json.dumps([{"name": "রহিম", "number": "01712345678"}], ensure_ascii=False))
        return CommandResult(code=0)


def make(runner=None, session=None, settings=None, screen_dir=None, control=None, **kwargs):
    contacts = control or SystemControl(runner=ContactsRunner())
    return AdbAutomation(
        control=contacts,
        runner=runner or FakeRunner(),
        settings=settings or FakeSettings(),
        session=session,
        screen_dir=screen_dir or Path(tempfile.mkdtemp()),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Number normalisation
# --------------------------------------------------------------------------- #

class TestInternational(unittest.TestCase):
    def test_bangladeshi_local_number(self):
        self.assertEqual(normalise_international("01712-345678"), "+8801712345678")

    def test_indian_ten_digit(self):
        self.assertEqual(normalise_international("9876543210"), "+919876543210")

    def test_already_international(self):
        self.assertEqual(normalise_international("+8801712345678"), "+8801712345678")
        self.assertEqual(normalise_international("8801712345678"), "+8801712345678")

    def test_bengali_digits(self):
        self.assertEqual(normalise_international("০১৭১২৩৪৫৬৭৮"), "+8801712345678")

    def test_empty(self):
        self.assertEqual(normalise_international(""), "")


# --------------------------------------------------------------------------- #
# open_app
# --------------------------------------------------------------------------- #

class TestOpenApp(unittest.TestCase):
    def test_bengali_app_names(self):
        cases = {
            "ইউটিউব": "com.google.android.youtube",
            "হোয়াটসঅ্যাপ": "com.whatsapp",
            "ফেসবুক": "com.facebook.katana",
            "টেলিগ্রাম": "org.telegram.messenger",
            "ক্রোম": "com.android.chrome",
            "সেটিংস": "com.android.settings",
            "ঘড়ি": "com.google.android.deskclock",
            "অ্যাঞ্জেল ওয়ান": "com.msf.angelone",
            "ক্যামেরা": "com.google.android.GoogleCamera",
        }
        for name, package in cases.items():
            with self.subTest(name=name):
                runner = FakeRunner()
                result = make(runner=runner).open_app(name)
                self.assertTrue(result["ok"], result.get("error"))
                self.assertEqual(result["package"], package)
                self.assertTrue(runner.count("monkey", "-p", package))
                self.assertIn("বস", result["message_bn"])

    def test_english_and_banglish_names(self):
        for name in ("YouTube", "youtube", "yt", "ফেসবুক অ্যাপটা খোলো", "open chrome"):
            with self.subTest(name=name):
                result = make().open_app(name)
                self.assertTrue(result["ok"], result.get("error"))

    def test_direct_package_name(self):
        result = make().open_app("com.whatsapp")
        self.assertTrue(result["ok"])
        self.assertEqual(result["app"], "whatsapp")

    def test_unknown_app(self):
        runner = FakeRunner()
        result = make(runner=runner).open_app("মাইক্রোসফট ওয়ার্ড")
        self.assertFalse(result["ok"])
        self.assertEqual(runner.calls, [])
        self.assertIn("চিনতে পারিনি", result["error"])
        self.assertIn("ইউটিউব", result["available"])
        self.assertIn("বস", result["message_bn"])

    def test_falls_back_when_monkey_finds_nothing(self):
        """monkey exits 0 but prints 'No activities found' -> try am start."""

        class PickyRunner(FakeRunner):
            def __call__(self, cmd, timeout=None):
                if cmd[2] == "monkey":
                    self.calls.append(list(cmd))
                    return CommandResult(code=0, stdout="** No activities found to run, monkey aborted.")
                return super().__call__(cmd, timeout)

        runner = PickyRunner()
        result = make(runner=runner).open_app("youtube")
        self.assertTrue(result["ok"])
        self.assertFalse(result["attempts"][0]["ok"])
        self.assertIn("am start", result["detail"])
        self.assertIn("-n", result["detail"])

    def test_every_launch_method_failing(self):
        runner = FakeRunner(fail_contains=("monkey", "am start"))
        result = make(runner=runner).open_app("settings")
        self.assertFalse(result["ok"])
        self.assertTrue(result["attempts"])
        self.assertIn("বস", result["message_bn"])

    def test_adb_missing(self):
        runner = FakeRunner(fail_contains=("monkey", "am start"))
        result = make(runner=runner).open_app("camera")
        self.assertFalse(result["ok"])
        self.assertIn("ক্যামেরা", result["message_bn"])


# --------------------------------------------------------------------------- #
# WhatsApp
# --------------------------------------------------------------------------- #

class TestWhatsApp(unittest.TestCase):
    def test_resolves_contact_and_builds_link(self):
        runner = FakeRunner()
        result = make(runner=runner).send_whatsapp(contact="রহিম", message="আসছি, অপেক্ষা করুন")

        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["name"], "রহিম")
        self.assertEqual(result["number"], "+8801712345678")
        self.assertIn("phone=8801712345678", result["url"])
        self.assertIn("text=%E0%A6%86%E0%A6%B8%E0%A6%9B%E0%A6%BF", result["url"])
        self.assertEqual(result["via"], "adb")
        self.assertTrue(runner.count("am", "start", "android.intent.action.VIEW", result["url"]))
        self.assertIn("রহিম", result["message_bn"])
        self.assertIn("বস", result["message_bn"])
        self.assertFalse(result["sent"])

    def test_number_argument_wins(self):
        result = make().send_whatsapp(number="+919876543210", message="hi")
        self.assertTrue(result["ok"])
        self.assertEqual(result["number"], "+919876543210")
        self.assertIn("phone=919876543210", result["url"])

    def test_unknown_contact_opens_nothing(self):
        runner = FakeRunner()
        result = make(runner=runner).send_whatsapp(contact="কেউ নেই")
        self.assertFalse(result["ok"])
        self.assertEqual(runner.calls, [])
        self.assertIn("বস", result["message_bn"])

    def test_empty_message_opens_chat_only(self):
        result = make().send_whatsapp(contact="রহিম")
        self.assertTrue(result["ok"])
        self.assertNotIn("text=", result["url"])
        self.assertIn("চ্যাট", result["message_bn"])

    def test_press_send_with_coordinates(self):
        runner = FakeRunner()
        result = make(runner=runner).send_whatsapp(
            contact="রহিম", message="পৌঁছেছি", press_send=True, send_button=(540, 1800)
        )
        self.assertTrue(result["sent"])
        self.assertEqual(result["send_attempt"]["strategy"], "tap")
        self.assertTrue(runner.count("input", "tap", "540", "1800"))
        self.assertIn("পাঠিয়ে", result["message_bn"])

    def test_press_send_without_coordinates_uses_enter(self):
        runner = FakeRunner()
        result = make(runner=runner).send_whatsapp(contact="রহিম", message="ok", press_send=True)
        self.assertEqual(result["send_attempt"]["strategy"], "keyevent-enter")
        self.assertTrue(runner.count("input", "keyevent", "66"))
        self.assertTrue(result["sent"])

    def test_termux_url_fallback_when_adb_intent_fails(self):
        runner = FakeRunner(fail_contains="android.intent.action.VIEW")
        result = make(runner=runner).send_whatsapp(contact="রহিম", message="hi")
        self.assertTrue(result["ok"])
        self.assertEqual(result["via"], "termux-open-url")
        self.assertTrue(runner.count("termux-open-url"))

    def test_both_openers_failing(self):
        class NoAdb(FakeRunner):
            def __call__(self, cmd, timeout=None):
                self.calls.append(list(cmd))
                if "am" in cmd:
                    return CommandResult(code=127, error="adb not found")
                return super().__call__(cmd, timeout)

        runner = NoAdb(fail_contains="termux-open-url")
        result = make(runner=runner).send_whatsapp(contact="রহিম", message="hi")
        self.assertFalse(result["ok"])
        self.assertIn("বস", result["message_bn"])
        self.assertEqual(len(result["attempts"]), 2)

    def test_invalid_send_coordinates(self):
        result = make().send_whatsapp(contact="রহিম", message="x", press_send=True, send_button=("a", "b"))
        self.assertFalse(result["send_attempt"]["ok"])
        self.assertFalse(result["sent"])


# --------------------------------------------------------------------------- #
# read_screen
# --------------------------------------------------------------------------- #

class TestReadScreen(unittest.TestCase):
    def test_screenshot_pull_and_vision(self):
        runner = FakeRunner()
        session = FakeSession()
        result = make(runner=runner, session=session).read_screen()

        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["model"], "gemini-2.5-flash")
        self.assertTrue(result["description_bn"].startswith("বস"))
        self.assertEqual(result["reply_bn"], result["description_bn"])
        self.assertTrue(runner.count("screencap", "-p", "/sdcard/orbit_screen.png"))
        self.assertTrue(runner.count("adb", "pull"))

        self.assertEqual(len(session.posts), 1)
        post = session.posts[0]
        self.assertIn("gemini-2.5-flash", post["url"])
        self.assertEqual(post["params"], {"key": "test-key"})
        parts = post["json"]["contents"][0]["parts"]
        self.assertIn("স্ক্রিনে কী আছে", parts[0]["text"])
        inline = parts[1]["inline_data"]
        self.assertEqual(inline["mime_type"], "image/png")
        self.assertTrue(base64.b64decode(inline["data"]).startswith(b"\x89PNG"))

    def test_screenshot_is_deleted_by_default(self):
        screen_dir = Path(tempfile.mkdtemp())
        result = make(session=FakeSession(), screen_dir=screen_dir).read_screen()
        self.assertTrue(result["ok"])
        self.assertEqual(list(screen_dir.glob("*.png")), [])
        self.assertFalse(result["image_kept"])

    def test_keep_screenshot(self):
        screen_dir = Path(tempfile.mkdtemp())
        result = make(session=FakeSession(), screen_dir=screen_dir, keep_screenshot=True).read_screen()
        self.assertTrue(result["ok"])
        self.assertEqual(len(list(screen_dir.glob("*.png"))), 1)

    def test_missing_api_key(self):
        session = FakeSession()
        result = make(session=session, settings=FakeSettings(gemini_key="")).read_screen()
        self.assertFalse(result["ok"])
        self.assertEqual(session.posts, [])
        self.assertIn("GEMINI_API_KEY", result["error"])
        self.assertIn("বস", result["reply_bn"])

    def test_screencap_failure(self):
        session = FakeSession()
        result = make(runner=FakeRunner(fail_contains="screencap"), session=session).read_screen()
        self.assertFalse(result["ok"])
        self.assertEqual(session.posts, [])
        self.assertIn("বস", result["reply_bn"])

    def test_pull_failure(self):
        result = make(runner=FakeRunner(pull_ok=False), session=FakeSession()).read_screen()
        self.assertFalse(result["ok"])
        self.assertIn("pull", result["error"])

    def test_tiny_file_is_not_a_screenshot(self):
        session = FakeSession()
        result = make(runner=FakeRunner(png=b"nope"), session=session).read_screen()
        self.assertFalse(result["ok"])
        self.assertIn("PNG", result["error"])
        self.assertEqual(session.posts, [])

    def test_vision_retries_once_on_500(self):
        session = FakeSession(statuses=[500, 200])
        result = make(session=session).read_screen()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(len(session.posts), 2)

    def test_vision_hard_failure(self):
        session = FakeSession(statuses=[400])
        result = make(session=session).read_screen()
        self.assertFalse(result["ok"])
        self.assertIn("400", result["error"])
        self.assertIn("বস", result["reply_bn"])

    def test_empty_vision_text(self):
        session = FakeSession(description="")
        result = make(session=session).read_screen()
        self.assertFalse(result["ok"])
        self.assertIn("empty", result["error"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
