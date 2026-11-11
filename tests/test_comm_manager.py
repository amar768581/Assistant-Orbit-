"""
tests/test_comm_manager.py — outgoing calls/SMS.

Contact resolution runs for real against a fake `termux-contact-list`, so the
name -> number path is exercised, not stubbed.
"""

from __future__ import annotations

import json
import unittest

from comm_manager import CommManager
from system_control import CommandResult, SystemControl

CONTACTS = [
    {"name": "রহিম", "number": "01712345678"},
    {"name": "Rahim Uddin", "number": "+8801812345678"},
    {"name": "করিম", "number": "01911111111"},
]


class FakeRunner:
    """Answers `termux-contact-list`; accepts everything else with exit 0."""

    def __init__(self, contacts=None, call_code=0, sms_code=0, error=None):
        self.contacts = CONTACTS if contacts is None else contacts
        self.call_code = call_code
        self.sms_code = sms_code
        self.error = error
        self.calls = []

    def __call__(self, cmd, timeout=None):
        self.calls.append(list(cmd))
        if self.error:
            return CommandResult(code=127, error=self.error)
        if cmd[0] == "termux-contact-list":
            return CommandResult(code=0, stdout=json.dumps(self.contacts, ensure_ascii=False))
        if cmd[0] == "termux-telephony-call" and self.call_code:
            return CommandResult(code=self.call_code, stderr="no CALL_PHONE permission")
        if cmd[0] == "termux-sms-send" and self.sms_code:
            return CommandResult(code=self.sms_code, stderr="no SEND_SMS permission")
        return CommandResult(code=0)

    def commands(self, name):
        return [c for c in self.calls if c and c[0] == name]


def make(runner, contacts=CONTACTS):
    return CommManager(
        control=SystemControl(runner=runner, contacts=contacts), runner=runner
    )


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #

class TestMakeCall(unittest.TestCase):
    def test_call_spoken_number(self):
        runner = FakeRunner()
        result = make(runner).make_call(number="০১৭১২৩৪৫৬৭৮")

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "call")
        self.assertEqual(result["number"], "01712345678")
        self.assertEqual(result["name"], "01712345678")
        self.assertEqual(result["source"], "spoken")
        self.assertEqual(runner.commands("termux-telephony-call"), [["termux-telephony-call", "01712345678"]])

    def test_call_contact_by_bengali_name(self):
        runner = FakeRunner()
        result = make(runner).make_call(contact="রহিম")

        self.assertTrue(result["ok"])
        self.assertEqual(result["number"], "01712345678")
        self.assertEqual(result["name"], "রহিম")
        self.assertEqual(result["source"], "contacts")
        self.assertIn("রহিম", result["message_bn"])

    def test_call_contact_by_banglish_name(self):
        runner = FakeRunner()
        result = make(runner).make_call(contact="rahim")
        self.assertTrue(result["ok"])
        self.assertIn(result["number"], ("01712345678", "+8801812345678"))

    def test_spoken_number_wins_over_name(self):
        runner = FakeRunner()
        result = make(runner).make_call(contact="রহিম", number="01600000000")
        self.assertTrue(result["ok"])
        self.assertEqual(result["number"], "01600000000")
        self.assertEqual(result["name"], "রহিম")

    def test_unknown_contact_does_not_dial(self):
        runner = FakeRunner()
        result = make(runner).make_call(contact="অচেনা কেউ")

        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "call")
        self.assertIn("contact not found", result["error"])
        self.assertEqual(runner.commands("termux-telephony-call"), [])

    def test_no_recipient(self):
        runner = FakeRunner()
        result = make(runner).make_call()
        self.assertFalse(result["ok"])
        self.assertIn("no contact or number", result["error"])
        self.assertEqual(runner.calls, [])

    def test_termux_call_failure(self):
        runner = FakeRunner(call_code=1)
        result = make(runner).make_call(contact="রহিম")

        self.assertFalse(result["ok"])
        self.assertIn("CALL_PHONE", result["error"])
        self.assertTrue(result["message_bn"].endswith("।"))

    def test_missing_termux_api(self):
        runner = FakeRunner(error="termux-telephony-call not found (pkg install termux-api)")
        result = make(runner).make_call(number="01712345678")

        self.assertFalse(result["ok"])
        self.assertIn("termux-api", result["error"])
        # A spoken number skips the address book, so the dial is attempted once.
        self.assertEqual(len(runner.commands("termux-telephony-call")), 1)

    def test_international_number_keeps_plus(self):
        runner = FakeRunner()
        result = make(runner).make_call(number="+8801712345678")
        self.assertTrue(result["ok"])
        self.assertEqual(result["number"], "+8801712345678")
        self.assertEqual(runner.commands("termux-telephony-call")[0][1], "+8801712345678")


# --------------------------------------------------------------------------- #
# SMS
# --------------------------------------------------------------------------- #

class TestSendSms(unittest.TestCase):
    def test_send_by_contact(self):
        runner = FakeRunner()
        result = make(runner).send_sms(contact="করিম", message="আসছি, অপেক্ষা করুন")

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "sms")
        self.assertEqual(result["number"], "01911111111")
        self.assertEqual(result["message"], "আসছি, অপেক্ষা করুন")
        self.assertEqual(
            runner.commands("termux-sms-send"),
            [["termux-sms-send", "-n", "01911111111", "আসছি, অপেক্ষা করুন"]],
        )

    def test_send_by_number(self):
        runner = FakeRunner()
        result = make(runner).send_sms(number="01799999999", message="ok")
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "01799999999")
        self.assertEqual(runner.commands("termux-sms-send")[0][1:3], ["-n", "01799999999"])

    def test_empty_message_is_refused(self):
        runner = FakeRunner()
        for body in ("", "   "):
            result = make(runner).send_sms(contact="রহিম", message=body)
            self.assertFalse(result["ok"])
            self.assertIn("মেসেজ", result["error"])
        self.assertEqual(runner.commands("termux-sms-send"), [])

    def test_unknown_contact(self):
        runner = FakeRunner()
        result = make(runner).send_sms(contact="নাই কেউ", message="হ্যালো")
        self.assertFalse(result["ok"])
        self.assertIn("contact not found", result["error"])
        self.assertEqual(runner.commands("termux-sms-send"), [])

    def test_sms_failure(self):
        runner = FakeRunner(sms_code=1)
        result = make(runner).send_sms(contact="রহিম", message="হ্যালো")
        self.assertFalse(result["ok"])
        self.assertIn("SEND_SMS", result["error"])
        self.assertIn("রহিম", result["message_bn"])

    def test_empty_address_book(self):
        runner = FakeRunner(contacts=[])
        result = make(runner, contacts=[]).send_sms(contact="রহিম", message="হ্যালো")
        self.assertFalse(result["ok"])
        self.assertEqual(runner.commands("termux-sms-send"), [])

    def test_message_preserves_bengali_and_punctuation(self):
        runner = FakeRunner()
        body = "বস, আজ ৫টায় মিটিং। দেরি হবে না!"
        result = make(runner).send_sms(contact="রহিম", message=body)
        self.assertTrue(result["ok"])
        self.assertEqual(runner.commands("termux-sms-send")[0][3], body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
