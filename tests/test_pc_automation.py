"""
tests/test_pc_automation.py — browser, folders, apps and system health with a
fake OS opener and a fake psutil: no window opens, no dependency is required.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from pc_automation import PcAutomation, _bn


class FakeBattery:
    def __init__(self, percent=87, plugged=True, secsleft=-1):
        self.percent = percent
        self.power_plugged = plugged
        self.secsleft = secsleft


class FakeProc:
    def __init__(self, name, rss):
        self.info = {"name": name, "memory_info": SimpleNamespace(rss=rss)}


class FakePsutil:
    def __init__(self, battery="default", fail=False):
        self.fail = fail
        self.battery = FakeBattery() if battery == "default" else battery
        self.calls = []

    def cpu_percent(self, interval=None):
        self.calls.append(("cpu", interval))
        if self.fail:
            raise RuntimeError("cpu exploded")
        return 42.5

    def cpu_count(self):
        return 8

    def virtual_memory(self):
        return SimpleNamespace(
            total=16 * 1024**3,
            used=8 * 1024**3,
            available=8 * 1024**3,
            percent=50.0,
        )

    def disk_usage(self, anchor):
        return SimpleNamespace(
            total=512 * 1024**3,
            used=312 * 1024**3,
            free=200 * 1024**3,
            percent=60.9,
        )

    def boot_time(self):
        return time.time() - 7200

    def sensors_battery(self):
        return self.battery

    def process_iter(self, fields):
        return [
            FakeProc("chrome.exe", 900 * 1024**2),
            FakeProc("python.exe", 300 * 1024**2),
            FakeProc("explorer.exe", 150 * 1024**2),
        ]


class Opener:
    def __init__(self, result=True):
        self.result = result
        self.opened = []

    def __call__(self, target):
        self.opened.append(target)
        return self.result


class Browser:
    def __init__(self, result=True):
        self.result = result
        self.opened = []

    def __call__(self, url):
        self.opened.append(url)
        return self.result


def make(**kwargs):
    opener = kwargs.pop("opener", Opener())
    browser = kwargs.pop("browser", Browser())
    psutil_module = kwargs.pop("psutil_module", FakePsutil())
    return (
        PcAutomation(
            opener=opener,
            browser=browser,
            psutil_module=psutil_module,
            platform_name=kwargs.pop("platform_name", "Windows"),
            **kwargs,
        ),
        {"opener": opener, "browser": browser, "psutil": psutil_module},
    )


class TestWebsite(unittest.TestCase):
    def test_https_is_assumed(self):
        pc, fakes = make()
        result = pc.open_website("github.com")
        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "https://github.com")
        self.assertEqual(fakes["browser"].opened, ["https://github.com"])

    def test_explicit_http_is_kept(self):
        pc, fakes = make()
        pc.open_website("http://example.com/a?b=1")
        self.assertEqual(fakes["browser"].opened, ["http://example.com/a?b=1"])

    def test_empty_and_unsupported_scheme(self):
        pc, fakes = make()
        self.assertFalse(pc.open_website("  ")["ok"])
        self.assertFalse(pc.open_website("file:///C:/secret.txt")["ok"])
        self.assertEqual(fakes["browser"].opened, [])

    def test_financial_site_is_blocked_before_opening(self):
        pc, fakes = make()
        for url in ("https://www.sbi.co.in", "phonepe.com", "paytm.com/login"):
            with self.subTest(url=url):
                result = pc.open_website(url)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "banking_app_blocked")
                self.assertIn("ব্যাংকিং", result["reply_bn"])
        self.assertEqual(fakes["browser"].opened, [])

    def test_browser_refusal_is_reported(self):
        pc, _ = make(browser=Browser(result=False))
        result = pc.open_website("example.com")
        self.assertFalse(result["ok"])
        self.assertIn("refused", result["error"])

    def test_browser_exception_is_swallowed(self):
        def broken(url):
            raise OSError("no browser")

        pc, _ = make(browser=broken)
        result = pc.open_website("example.com")
        self.assertFalse(result["ok"])
        self.assertIn("no browser", result["error"])


class TestFolders(unittest.TestCase):
    def test_existing_folder_opens_resolved(self):
        pc, fakes = make()
        with tempfile.TemporaryDirectory() as tmp:
            result = pc.open_folder(tmp)
        self.assertTrue(result["ok"])
        self.assertEqual(len(fakes["opener"].opened), 1)
        self.assertIn("ফোল্ডার", result["message_bn"])

    def test_missing_folder_never_opens(self):
        pc, fakes = make()
        result = pc.open_folder("C:/definitely/not/here")
        self.assertFalse(result["ok"])
        self.assertIn("পাওয়া যায়নি", result["message_bn"])
        self.assertEqual(fakes["opener"].opened, [])

    def test_file_is_not_a_folder(self):
        pc, fakes = make()
        with tempfile.NamedTemporaryFile() as handle:
            result = pc.open_folder(handle.name)
        self.assertFalse(result["ok"])
        self.assertIn("ফোল্ডার নয়", result["message_bn"])
        self.assertEqual(fakes["opener"].opened, [])

    def test_empty_path(self):
        pc, _ = make()
        self.assertFalse(pc.open_folder("   ")["ok"])

    def test_expand_path_handles_home_and_variables(self):
        self.assertEqual(PcAutomation.expand_path("~"), Path.home())
        self.assertTrue(str(PcAutomation.expand_path("~/Downloads")).startswith(str(Path.home())))
        self.assertTrue(PcAutomation.expand_path("%USERPROFILE%").name)

    def test_opener_failure_is_reported(self):
        pc, _ = make(opener=Opener(result=False))
        with tempfile.TemporaryDirectory() as tmp:
            result = pc.open_folder(tmp)
        self.assertFalse(result["ok"])
        self.assertIn("খোলা গেল না", result["message_bn"])


class TestApps(unittest.TestCase):
    def test_friendly_alias(self):
        pc, fakes = make()
        result = pc.open_app("notepad")
        self.assertTrue(result["ok"])
        self.assertEqual(fakes["opener"].opened, ["notepad.exe"])

    def test_bengali_alias(self):
        pc, fakes = make()
        pc.open_app("ক্যালকুলেটর")
        self.assertEqual(fakes["opener"].opened, ["calc.exe"])

    def test_financial_app_is_blocked(self):
        pc, fakes = make()
        for name in ("SBI YONO", "PhonePe", "Angel One", "ব্যাংক অ্যাপ"):
            with self.subTest(name=name):
                result = pc.open_app(name)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "banking_app_blocked")
                self.assertIn("ব্যাংকিং", result["reply_bn"])
        self.assertEqual(fakes["opener"].opened, [])

    def test_empty_name(self):
        pc, _ = make()
        self.assertFalse(pc.open_app("  ")["ok"])

    def test_unknown_name_becomes_exe_on_windows(self):
        pc, fakes = make()
        pc.open_app("MyCompanyTool")
        self.assertEqual(fakes["opener"].opened, ["MyCompanyTool.exe"])

    def test_path_is_passed_through(self):
        pc, fakes = make()
        pc.open_app(r"C:\\Tools\\app.exe")
        self.assertEqual(fakes["opener"].opened, [r"C:\\Tools\\app.exe"])

    def test_launch_failure(self):
        pc, _ = make(opener=Opener(result=False))
        result = pc.open_app("notepad")
        self.assertFalse(result["ok"])
        self.assertIn("খোলা গেল না", result["message_bn"])


class TestHealth(unittest.TestCase):
    def test_stats_and_summary(self):
        pc, _ = make()
        stats = pc.system_health()
        self.assertTrue(stats["ok"])
        self.assertEqual(stats["cpu_percent"], 42.5)
        self.assertEqual(stats["ram_total_gb"], 16.0)
        self.assertEqual(stats["ram_used_gb"], 8.0)
        self.assertEqual(stats["disk_free_gb"], 200.0)
        self.assertEqual(stats["battery_percent"], 87)
        self.assertTrue(stats["battery_plugged"])
        self.assertIn("সিপিইউ ৪২.৫", stats["summary_bn"])
        self.assertIn("র‍্যাম ৮.০ / ১৬.০", stats["summary_bn"])
        self.assertIn("ব্যাটারি ৮৭", stats["summary_bn"])
        self.assertEqual(
            [row["name"] for row in stats["top_processes"]],
            ["chrome.exe", "python.exe", "explorer.exe"],
        )

    def test_no_battery_desktop(self):
        pc, _ = make(psutil_module=FakePsutil(battery=None))
        stats = pc.system_health()
        self.assertIsNone(stats["battery_percent"])
        self.assertIn("ব্যাটারি নেই", stats["summary_bn"])

    def test_psutil_missing_is_an_error_dict(self):
        class Broken:
            def __getattr__(self, item):
                raise AssertionError("should not be called")

        pc = PcAutomation(opener=Opener(), platform_name="Windows")
        pc._psutil = Broken()
        result = pc.system_health()
        self.assertFalse(result["ok"])

    def test_cpu_failure_is_reported(self):
        pc, _ = make(psutil_module=FakePsutil(fail=True))
        result = pc.system_health()
        self.assertFalse(result["ok"])
        self.assertIn("system stats failed", result["error"])

    def test_bengali_digits(self):
        self.assertEqual(_bn("42.5"), "৪২.৫")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
