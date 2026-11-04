"""
tests/test_security_guard.py — unit tests for ORBIT's intruder check.

The camera is simulated by a scripted runner that writes a fake JPEG to the
path Termux would have used, so no camera, GPS or Termux:API is required.

    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from security_guard import (
    FRONT_CAMERA,
    REAR_CAMERA,
    SecurityGuard,
    _bn_number,
)
from system_control import CommandResult

FIXED_TIME = datetime(2026, 11, 4, 19, 45, 0, tzinfo=timezone.utc)

LOCATION = {
    "latitude": 23.8104,
    "longitude": 90.4126,
    "accuracy": 20.0,
    "provider": "network",
    "elapsedMs": 12,
}


class ScriptedRunner:
    """Calls a handler and records the commands (like a Termux shell)."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[list[str]] = []

    def __call__(self, cmd, timeout=None):
        self.calls.append(list(cmd))
        return self.handler(list(cmd), timeout)

    # convenience
    @property
    def camera_calls(self):
        return [c for c in self.calls if c and c[0] == "termux-camera-photo"]

    @property
    def location_calls(self):
        return [c for c in self.calls if c and c[0] == "termux-location"]


def make_runner(
    working_cameras=(FRONT_CAMERA,),
    cameras_fail_hard=False,
    payload=None,
    photo_bytes=4096,
    location_error=None,
):
    """Build a runner that behaves like a phone with the given hardware state."""

    def handler(cmd, _timeout):
        if cmd[0] == "termux-camera-photo":
            camera_id = int(cmd[2])
            target = Path(cmd[-1])
            if cameras_fail_hard:
                return CommandResult(code=127, error="termux-camera-photo not found (pkg install termux-api)")
            if camera_id in working_cameras:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"\xff\xd8\xff" + b"\0" * max(0, photo_bytes - 3))
                return CommandResult(code=0)
            return CommandResult(code=1, stderr="camera in use")

        if cmd[0] == "termux-location":
            if location_error:
                return location_error
            return CommandResult(code=0, stdout=json.dumps(payload if payload is not None else LOCATION))

        return CommandResult(code=0)

    return ScriptedRunner(handler)


def guard(runner, **kwargs) -> SecurityGuard:
    tmp = tempfile.mkdtemp(prefix="orbit_intruder_")
    return SecurityGuard(
        photo_dir=Path(tmp),
        runner=runner,
        clock=lambda: FIXED_TIME,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

class TestHelpers(unittest.TestCase):
    def test_bn_number(self):
        self.assertEqual(_bn_number(23.8104), "২৩.৮১০৪০০")
        self.assertEqual(_bn_number(-0.5, places=2), "-০.৫০")

    def test_maps_url(self):
        self.assertEqual(
            SecurityGuard.maps_url(23.8104, 90.4126),
            "https://maps.google.com/?q=23.810400,90.412600",
        )
        self.assertEqual(
            SecurityGuard.maps_url(-33.8688, 151.2093),
            "https://maps.google.com/?q=-33.868800,151.209300",
        )


# --------------------------------------------------------------------------- #
# Camera
# --------------------------------------------------------------------------- #

class TestCamera(unittest.TestCase):
    def test_front_camera_capture(self):
        runner = make_runner()
        g = guard(runner)
        result = g.capture_photo(FRONT_CAMERA)

        self.assertTrue(result["ok"])
        self.assertEqual(result["camera"], FRONT_CAMERA)
        self.assertEqual(result["bytes"], 4096)
        path = Path(result["path"])
        self.assertTrue(path.is_file())
        self.assertIn("front", path.name)
        self.assertEqual(path.parent, g.photo_dir)
        self.assertEqual(runner.camera_calls[0][1:3], ["-c", "1"])

    def test_failed_camera_without_file(self):
        runner = make_runner(working_cameras=())
        result = guard(runner).capture_photo(FRONT_CAMERA)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["path"])
        self.assertIn("camera in use", result["error"])

    def test_tiny_file_is_discarded(self):
        runner = make_runner(photo_bytes=10)
        g = guard(runner)
        result = g.capture_photo(FRONT_CAMERA)
        self.assertFalse(result["ok"])
        self.assertIn("no image", result["error"])
        self.assertEqual(list(g.photo_dir.glob("*.jpg")), [])

    def test_missing_binary(self):
        runner = make_runner(cameras_fail_hard=True)
        result = guard(runner).capture_photo(FRONT_CAMERA)
        self.assertFalse(result["ok"])
        self.assertIn("termux-api", result["error"])

    def test_rear_camera_label(self):
        runner = make_runner(working_cameras=(REAR_CAMERA,))
        result = guard(runner).capture_photo(REAR_CAMERA)
        self.assertTrue(result["ok"])
        self.assertIn("rear", Path(result["path"]).name)


# --------------------------------------------------------------------------- #
# Location
# --------------------------------------------------------------------------- #

class TestLocation(unittest.TestCase):
    def test_network_fix(self):
        result = guard(make_runner()).locate()
        self.assertTrue(result["ok"])
        self.assertEqual(result["lat"], 23.8104)
        self.assertEqual(result["lon"], 90.4126)
        self.assertEqual(result["accuracy"], 20.0)
        self.assertEqual(result["provider"], "network")
        self.assertEqual(result["maps_url"], "https://maps.google.com/?q=23.810400,90.412600")
        self.assertEqual(result["attempts"], [])

    def test_gps_fallback(self):
        runner = make_runner(
            payload={**LOCATION, "provider": "gps"},
            location_error=None,
        )

        # network fails the first time only, gps succeeds afterwards
        state = {"calls": 0}
        original = runner.handler

        def handler(cmd, timeout):
            if cmd[0] == "termux-location" and state["calls"] == 0:
                state["calls"] += 1
                return CommandResult(code=1, stderr="network provider disabled")
            state["calls"] += 1
            return original(cmd, timeout)

        runner.handler = handler
        result = guard(runner).locate()
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "gps")
        self.assertEqual(len(result["attempts"]), 1)
        self.assertIn("network provider disabled", result["attempts"][0]["error"])
        self.assertEqual([c[2] for c in runner.location_calls], ["network", "gps"])

    def test_all_providers_fail(self):
        runner = make_runner(location_error=CommandResult(code=1, stderr="location permission denied"))
        result = guard(runner).locate()
        self.assertFalse(result["ok"])
        self.assertIsNone(result["lat"])
        self.assertIsNone(result["maps_url"])
        self.assertEqual(len(result["attempts"]), 2)
        self.assertIn("location permission denied", result["error"])
        self.assertEqual([c[2] for c in runner.location_calls], ["network", "gps"])

    def test_null_and_out_of_range_coords(self):
        nulls = make_runner(payload={**LOCATION, "latitude": None})
        self.assertFalse(guard(nulls).locate()["ok"])

        bad = make_runner(payload={**LOCATION, "latitude": 123.0})
        result = guard(bad).locate()
        self.assertFalse(result["ok"])
        self.assertIn("out-of-range", result["attempts"][0]["error"])

    def test_unreadable_json(self):
        runner = make_runner()
        runner.handler = lambda cmd, timeout: CommandResult(code=0, stdout="not json")
        result = guard(runner).locate(providers=("network",))
        self.assertFalse(result["ok"])
        self.assertIn("unreadable JSON", result["attempts"][0]["error"])

    def test_custom_provider_order(self):
        runner = make_runner()
        guard(runner).locate(providers=("gps",))
        self.assertEqual([c[2] for c in runner.location_calls], ["gps"])


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #

class TestSummary(unittest.TestCase):
    def setUp(self):
        self.g = guard(make_runner())

    def test_photo_and_location(self):
        photo = {"ok": True, "camera": FRONT_CAMERA, "path": "x.jpg"}
        location = {"ok": True, "lat": 23.8104, "lon": 90.4126, "accuracy": 20.0}
        summary = self.g.build_summary(photo, location, FIXED_TIME)

        self.assertIn("সতর্কতা", summary)
        self.assertIn("সামনের ক্যামেরায় ছবি তোলা হয়েছে", summary)
        self.assertIn("অক্ষাংশ ২৩.৮১০৪০০", summary)
        self.assertIn("দ্রাঘিমাংশ ৯০.৪১২৬০০", summary)
        self.assertIn("২০ মিটার", summary)
        self.assertIn("গুগল ম্যাপের লিংক", summary)
        self.assertIn("১৯:৪৫", summary)

    def test_no_photo(self):
        location = {"ok": True, "lat": 23.8104, "lon": 90.4126, "accuracy": None}
        summary = self.g.build_summary(None, location, FIXED_TIME)
        self.assertIn("ছবি তোলা যায়নি", summary)
        self.assertIn("অক্ষাংশ", summary)
        self.assertNotIn("গুগল ম্যাপের লিংক পাঠাচ্ছি। অবস্থান", summary)

    def test_no_location(self):
        photo = {"ok": True, "camera": REAR_CAMERA, "path": "x.jpg"}
        summary = self.g.build_summary(photo, {"ok": False, "lat": None, "lon": None}, FIXED_TIME)
        self.assertIn("পেছনের ক্যামেরায়", summary)
        self.assertIn("অবস্থান পাওয়া যায়নি", summary)

    def test_nothing_captured(self):
        summary = self.g.build_summary(None, {"ok": False}, FIXED_TIME)
        self.assertIn("ছবি তোলা যায়নি", summary)
        self.assertIn("অবস্থান পাওয়া যায়নি", summary)

    def test_photo_not_requested(self):
        summary = self.g.build_summary(None, {"ok": True, "lat": 1.0, "lon": 2.0}, FIXED_TIME, photo_attempted=False)
        self.assertIn("ছবি তোলার অনুরোধ ছিল না", summary)
        self.assertNotIn("ছবি তোলা যায়নি", summary)


# --------------------------------------------------------------------------- #
# intruder_check
# --------------------------------------------------------------------------- #

class TestIntruderCheck(unittest.TestCase):
    def test_full_success(self):
        runner = make_runner()
        g = guard(runner)
        result = g.intruder_check()

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "intruder_check")
        self.assertTrue(Path(result["photo_path"]).is_file())
        self.assertEqual(result["camera"], FRONT_CAMERA)
        self.assertEqual(result["lat"], 23.8104)
        self.assertEqual(result["lon"], 90.4126)
        self.assertEqual(result["maps_url"], "https://maps.google.com/?q=23.810400,90.412600")
        self.assertTrue(result["location_ok"])
        self.assertEqual(result["timestamp"], FIXED_TIME.isoformat(timespec="seconds"))
        self.assertEqual(result["epoch"], int(FIXED_TIME.timestamp()))
        self.assertIn("সতর্কতা", result["summary_bn"])
        self.assertEqual(result["attempts"]["cameras"][0], {"camera": 1, "ok": True, "error": None})
        json.dumps(result, ensure_ascii=False)   # Telegram-ready
        self.assertIs(g.last_result, result)
        self.assertEqual(len(g.history), 1)

    def test_front_camera_failure_falls_back_to_rear(self):
        result = guard(make_runner(working_cameras=(REAR_CAMERA,))).intruder_check()
        self.assertTrue(result["ok"])
        self.assertEqual(result["camera"], REAR_CAMERA)
        self.assertEqual([a["camera"] for a in result["attempts"]["cameras"]], [1, 0])
        self.assertFalse(result["attempts"]["cameras"][0]["ok"])
        self.assertIn("পেছনের ক্যামেরায়", result["summary_bn"])

    def test_no_capture_flag_skips_camera(self):
        runner = make_runner()
        result = guard(runner).intruder_check(capture=False)
        self.assertEqual(runner.camera_calls, [])
        self.assertFalse(result["photo_attempted"])
        self.assertIsNone(result["photo_path"])
        self.assertTrue(result["location_ok"])
        self.assertTrue(result["ok"])
        self.assertIn("ছবি তোলার অনুরোধ ছিল না", result["summary_bn"])

    def test_both_fail_reports_error(self):
        runner = make_runner(
            working_cameras=(),
            location_error=CommandResult(code=1, stderr="no gps"),
        )
        g = guard(runner)
        result = g.intruder_check()
        self.assertFalse(result["ok"])
        self.assertIsNone(result["photo_path"])
        self.assertIsNone(result["maps_url"])
        self.assertIn("neither a photo nor a location", result["error"])
        self.assertEqual(g.last_error, result["error"])
        self.assertIn("ছবি তোলা যায়নি", result["summary_bn"])

    def test_history_is_capped(self):
        g = guard(make_runner())
        for _ in range(25):
            g.intruder_check(capture=False)
        self.assertEqual(len(g.history), 20)

    def test_custom_camera_order(self):
        result = guard(make_runner(working_cameras=(REAR_CAMERA,))).intruder_check()
        self.assertEqual(result["camera"], REAR_CAMERA)

        runner = make_runner(working_cameras=(FRONT_CAMERA,))
        g = SecurityGuard(photo_dir=Path(tempfile.mkdtemp()), runner=runner, cameras=(REAR_CAMERA, FRONT_CAMERA), clock=lambda: FIXED_TIME)
        result = g.intruder_check()
        self.assertEqual(result["camera"], FRONT_CAMERA)
        self.assertEqual([a["camera"] for a in result["attempts"]["cameras"]], [0, 1])


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
