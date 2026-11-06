"""
tests/test_security_policy.py — one guard, many disguises: bank packages,
Bengali app names, OTP/PIN text, bank alerts and FLAG_SECURE windows.
"""

from __future__ import annotations

import unittest

import security_policy as sp


class TestBlockLists(unittest.TestCase):
    def test_every_app_package_is_in_the_package_set(self):
        for name, packages in sp.BLOCKED_FINANCIAL_APPS.items():
            self.assertTrue(packages, name)
            for package in packages:
                self.assertIn(package, sp.BLOCKED_PACKAGES)

    def test_known_financial_apps_are_blocked(self):
        for name in (
            "SBI YONO", "HDFC Bank", "ICICI Bank", "Axis Bank", "Kotak",
            "PNB", "Bank of Baroda", "Google Pay", "PhonePe", "Paytm",
            "BHIM", "Amazon Pay", "CRED", "Angel One", "Zerodha Kite", "Groww",
        ):
            with self.subTest(name=name):
                self.assertTrue(sp.is_financial_target(name), name)

    def test_known_packages_are_blocked(self):
        for package in (
            "com.sbi.lotusintouch",
            "com.google.android.apps.nbu.paisa.user",
            "com.phonepe.app",
            "net.one97.paytm",
            "in.org.npci.upiapp",
            "in.amazon.mShop.android.shopping",
            "com.dreamplug.androidapp",
            "com.msf.angelone",
            "com.zerodhatech.kite",
            "com.nextbillion.groww",
        ):
            self.assertTrue(sp.is_financial_package(package), package)

    def test_keyword_packages_are_blocked(self):
        for package in (
            "com.example.mobilebanking",
            "com.example.netbanking",
            "in.example.upi",
            "com.example.wallet",
            "com.example.finance",
            "com.example.payments",
        ):
            self.assertTrue(sp.is_financial_package(package), package)

    def test_normal_apps_are_not_blocked(self):
        for target in ("YouTube", "হোয়াটসঅ্যাপ", "Telegram", "Chrome", "Camera", "Settings",
                       "com.whatsapp", "com.google.android.youtube", "org.telegram.messenger",
                       "com.android.settings", "com.android.chrome"):
            with self.subTest(target=target):
                self.assertFalse(sp.is_financial_target(target), target)

    def test_bengali_financial_names(self):
        for text in ("ব্যাংক অ্যাপ", "পেমেন্ট অ্যাপ", "ওয়ালেট", "শেয়ার বাজার"):
            self.assertTrue(sp.is_financial_app_name(text), text)

    def test_refusal_shape(self):
        blocked = sp.financial_app_block("SBI YONO")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"], "banking_app_blocked")
        self.assertEqual(blocked["reply_bn"], sp.BANKING_REFUSAL_BN)
        self.assertTrue(blocked["reply_bn"].startswith("বস"))
        self.assertIn("ব্যাংকিং", blocked["reply_bn"])


class TestSecrets(unittest.TestCase):
    def test_english_secrets(self):
        for text in (
            "Your OTP is 445566",
            "OTP: 445566",
            "one-time password 1234",
            "Your verification code is 998877",
            "CVV 123",
            "ATM PIN 4321",
            "your password is hunter2",
            "passcode 1111",
        ):
            self.assertTrue(sp.contains_secret(text), text)

    def test_bengali_secrets(self):
        for text in (
            "আপনার ওটিপি ৪৪৫৫৬৬",
            "ও টি পি পাঠান",
            "পিন নম্বর দিন",
            "আপনার পাসওয়ার্ড দিন",
            "গোপন কোড ১২৩৪",
            "ভেরিফিকেশন কোড ৯৯৮৮",
        ):
            self.assertTrue(sp.contains_secret(text), text)

    def test_secret_lookalikes_are_not_matched(self):
        for text in ("pinned message", "spinning wheel", "opinion poll", "কাল দেখা হবে?"):
            self.assertFalse(sp.contains_secret(text), text)

    def test_bank_alerts(self):
        for text in (
            "Rs.500 debited from your a/c",
            "Your account credited with Rs 1000",
            "UPI transaction successful",
            "NEFT transfer complete",
            "Available balance is Rs 250",
            "আপনার অ্যাকাউন্টে ৫০০ টাকা ডেবিট হয়েছে",
            "ব্যালেন্স কমেছে",
        ):
            self.assertTrue(sp.is_bank_alert(text), text)

    def test_normal_message_is_not_a_bank_alert(self):
        for text in ("কাল দেখা হবে?", "ফাইল পাঠিয়েছি", "Download complete"):
            self.assertFalse(sp.is_bank_alert(text), text)

    def test_is_sensitive_text_combines_both(self):
        self.assertTrue(sp.is_sensitive_text("OTP 1234"))
        self.assertTrue(sp.is_sensitive_text("debited"))
        self.assertFalse(sp.is_sensitive_text("hello"))

    def test_sensitive_notification(self):
        self.assertTrue(sp.is_sensitive_notification(
            {"packageName": "com.phonepe.app", "title": "PhonePe", "content": "paid"}
        ))
        self.assertTrue(sp.is_sensitive_notification(
            {"packageName": "com.whatsapp", "title": "Bank", "content": "OTP 445566"}
        ))
        self.assertTrue(sp.is_sensitive_notification(
            {"packageName": "com.android.messaging", "content": "Rs 500 debited"}
        ))
        self.assertFalse(sp.is_sensitive_notification(
            {"packageName": "com.whatsapp", "title": "রহিম", "content": "কাল দেখা হবে?"}
        ))

    def test_redaction(self):
        redacted = sp.redact_sensitive("Your OTP is 445566 and PIN 1234")
        self.assertNotIn("445566", redacted)
        self.assertNotIn("1234", redacted)
        self.assertIn("[redacted]", redacted)

    def test_financial_requests(self):
        for text in (
            "SBI YONO খোলো",
            "ব্যাংক অ্যাপে ব্যালেন্স দেখো",
            "আমার OTP বলো",
            "UPI দিয়ে টাকা পাঠাও",
            "net banking login করো",
        ):
            self.assertTrue(sp.is_financial_request(text), text)

    def test_normal_requests_are_not_financial(self):
        for text in ("টর্চ জালাও", "আজকের বাজার কেমন?", "ফেসবুকে লগইন করো", "ইউটিউব খোলো"):
            self.assertFalse(sp.is_financial_request(text), text)


class TestScreenGuard(unittest.TestCase):
    WHATSAPP = (
        "WINDOW MANAGER WINDOWS (dumpsys window windows)\n"
        "  mCurrentFocus=Window{2a3b u0 com.whatsapp/com.whatsapp.HomeActivity}\n"
        "  Window #4 Window{2a3b u0 com.whatsapp/com.whatsapp.HomeActivity}:\n"
        "    mAttrs={(0,0)(fillxfill)}\n"
    )
    SBI = (
        "  mCurrentFocus=Window{9f1 u0 com.sbi.lotusintouch/com.sbi.lotusintouch.MainActivity}\n"
    )
    SECURE = (
        "  mCurrentFocus=Window{9f1 u0 com.android.chrome/com.google.android.apps.chrome.Main}\n"
        "    mAttrs={(0,0)(fillxfill) ty=APPLICATION}\n"
        "    fl=FLAG_SECURE\n"
    )
    FOCUSED_APP = (
        "  mFocusedApp=ActivityRecord{1a2b u0 com.phonepe.app/.MainActivity t42}\n"
    )

    def test_whatsapp_is_safe(self):
        info = sp.parse_foreground_window(self.WHATSAPP)
        self.assertEqual(info["package"], "com.whatsapp")
        self.assertFalse(info["blocked"])
        self.assertTrue(info["ok"])

    def test_financial_foreground_is_blocked(self):
        info = sp.evaluate_screen(self.SBI)
        self.assertTrue(info["blocked"])
        self.assertEqual(info["reason"], "financial_app")
        self.assertEqual(info["package"], "com.sbi.lotusintouch")
        self.assertEqual(info["reply_bn"], sp.SCREEN_REFUSAL_BN)

    def test_flag_secure_is_blocked_even_in_a_normal_app(self):
        info = sp.evaluate_screen(self.SECURE)
        self.assertTrue(info["blocked"])
        self.assertEqual(info["reason"], "flag_secure")
        self.assertTrue(info["secure"])

    def test_mfocusedapp_fallback(self):
        info = sp.evaluate_screen(self.FOCUSED_APP)
        self.assertTrue(info["blocked"])
        self.assertEqual(info["package"], "com.phonepe.app")

    def test_unavailable_dumpsys_fails_closed(self):
        for text in ("", "   ", "garbage without a window"):
            info = sp.evaluate_screen(text)
            self.assertTrue(info["blocked"], text)
            self.assertEqual(info["reason"], "guard_unavailable")
            self.assertEqual(info["reply_bn"], sp.SCREEN_REFUSAL_BN)

    def test_safe_screen_has_no_refusal(self):
        info = sp.evaluate_screen(self.WHATSAPP)
        self.assertFalse(info["blocked"])
        self.assertNotIn("reply_bn", info)


class TestReport(unittest.TestCase):
    def test_report_is_informative(self):
        report = sp.security_report()
        self.assertTrue(report["ok"])
        self.assertTrue(report["enabled"])
        self.assertEqual(report["apps"], len(sp.BLOCKED_FINANCIAL_APPS))
        self.assertEqual(report["packages"], len(sp.BLOCKED_PACKAGES))
        self.assertIn("OTP", report["label_bn"])
        self.assertIn("ব্লকড", report["label_bn"])

    def test_blocked_app_names(self):
        names = list(sp.blocked_app_names())
        self.assertIn("sbi yono", names)
        self.assertIn("google pay", names)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
