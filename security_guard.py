"""
security_guard.py — ORBIT's intruder check.

One call gathers the evidence a worried phone owner actually needs:

    1. a silent photo from the front camera (`termux-camera-photo -c 1`),
       falling back to the rear camera (`-c 0`),
    2. the device location (`termux-location -p network`, then `-p gps`),
    3. a ready-to-tap Google Maps link,
    4. a Bengali one-liner that can be spoken or sent to Telegram.

    from security_guard import SecurityGuard
    guard = SecurityGuard()
    result = guard.intruder_check()
    result["photo_path"]   # '.intruder/intruder_1793..._front.jpg' (or None)
    result["maps_url"]     # 'https://maps.google.com/?q=23.810400,90.412600'
    result["summary_bn"]   # spoken by voice_engine / sent as Telegram caption

Photos land in ``.intruder/`` which is git-ignored. Nothing here raises: a
denied camera permission or a disabled GPS still returns a usable dict.

CLI:
    python security_guard.py               # photo + location
    python security_guard.py --no-photo    # location only
    python security_guard.py --camera 0    # force the rear camera
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from config import BASE_DIR, settings as default_settings
from system_control import (
    CommandResult,
    _extract_json,
    run_command,
    to_bengali_digits,
    which_binary,
)

log = logging.getLogger("orbit.security")

PHOTO_DIR = BASE_DIR / ".intruder"
MAPS_URL = "https://maps.google.com/?q={lat:.6f},{lon:.6f}"

FRONT_CAMERA = 1
REAR_CAMERA = 0
DEFAULT_CAMERAS: Sequence[int] = (FRONT_CAMERA, REAR_CAMERA)
DEFAULT_PROVIDERS: Sequence[str] = ("network", "gps")

CAMERA_TIMEOUT = 25.0
LOCATION_TIMEOUT = 35.0
MIN_PHOTO_BYTES = 1024  # smaller than this is a failed/empty capture

HISTORY_LIMIT = 20


def _bn_number(value: float, places: int = 6) -> str:
    """23.8104 -> '২৩.৮১০৪০০' (sign and dot preserved)."""
    return to_bengali_digits(f"{value:.{places}f}")


class SecurityGuard:
    """Silent photo + GPS evidence package for ``intruder_check``."""

    def __init__(
        self,
        settings: Optional[Any] = None,
        photo_dir: Path = PHOTO_DIR,
        cameras: Sequence[int] = DEFAULT_CAMERAS,
        timeout: float = CAMERA_TIMEOUT,
        location_timeout: float = LOCATION_TIMEOUT,
        runner: Callable[..., CommandResult] = run_command,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.settings = settings or default_settings
        self.photo_dir = Path(photo_dir)
        self.cameras = tuple(int(c) for c in cameras) or DEFAULT_CAMERAS
        self.timeout = timeout
        self.location_timeout = location_timeout
        self._runner = runner
        self._clock = clock or (lambda: datetime.now().astimezone())

        self.last_result: Optional[Dict[str, Any]] = None
        self.history: list[Dict[str, Any]] = []
        self.last_error: Optional[str] = None

    # -- plumbing ---------------------------------------------------------- #

    def _now(self) -> datetime:
        return self._clock()

    def _run(self, cmd: Sequence[str], timeout: Optional[float] = None) -> CommandResult:
        try:
            return self._runner(list(cmd), timeout or self.timeout)
        except TypeError:  # a fake runner that only accepts the command
            return self._runner(list(cmd))

    @staticmethod
    def maps_url(lat: float, lon: float) -> str:
        """Direct Google Maps pin for the coordinates."""
        return MAPS_URL.format(lat=float(lat), lon=float(lon))

    # ------------------------------------------------------------------ #
    # Camera
    # ------------------------------------------------------------------ #

    def capture_photo(self, camera_id: int = FRONT_CAMERA, when: Optional[datetime] = None) -> Dict[str, Any]:
        """Take one silent photo. Returns ``{'ok', 'path', 'camera', ...}``."""
        stamp = when or self._now()
        label = "front" if camera_id == FRONT_CAMERA else "rear" if camera_id == REAR_CAMERA else f"cam{camera_id}"

        try:
            self.photo_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.last_error = f"cannot create {self.photo_dir}: {exc}"
            log.warning(self.last_error)
            return {"ok": False, "camera": camera_id, "error": self.last_error}

        target = self.photo_dir / f"intruder_{int(stamp.timestamp())}_{label}.jpg"
        target.unlink(missing_ok=True)

        result = self._run(
            ["termux-camera-photo", "-c", str(camera_id), str(target)],
            timeout=self.timeout,
        )

        size = target.stat().st_size if target.is_file() else 0
        if result.ok and size >= MIN_PHOTO_BYTES:
            log.info("Intruder photo saved: %s (%d bytes, camera %d)", target.name, size, camera_id)
            return {
                "ok": True,
                "camera": camera_id,
                "path": str(target),
                "bytes": size,
                "error": None,
            }

        if target.is_file() and size < MIN_PHOTO_BYTES:
            target.unlink(missing_ok=True)

        if result.ok:
            error = f"camera {camera_id} produced no image (permission denied?)"
        else:
            error = result.describe()
        log.warning("Camera %d failed: %s", camera_id, error)
        return {"ok": False, "camera": camera_id, "path": None, "error": error}

    # ------------------------------------------------------------------ #
    # Location
    # ------------------------------------------------------------------ #

    def locate(self, providers: Sequence[str] = DEFAULT_PROVIDERS) -> Dict[str, Any]:
        """Try each provider in order; return the first usable fix."""
        attempts: list[Dict[str, Any]] = []

        for provider in providers:
            result = self._run(
                ["termux-location", "-p", provider],
                timeout=self.location_timeout,
            )
            if not result.ok:
                attempts.append({"provider": provider, "ok": False, "error": result.describe()})
                continue

            try:
                data = _extract_json(result.output)
            except ValueError as exc:
                attempts.append({"provider": provider, "ok": False, "error": f"unreadable JSON: {exc}"})
                continue

            if not isinstance(data, dict):
                attempts.append({"provider": provider, "ok": False, "error": "unexpected payload"})
                continue

            lat, lon = data.get("latitude"), data.get("longitude")
            try:
                lat_f, lon_f = float(lat), float(lon)
            except (TypeError, ValueError):
                attempts.append({"provider": provider, "ok": False, "error": "no coordinates (location off?)"})
                continue
            if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
                attempts.append({"provider": provider, "ok": False, "error": f"out-of-range coords {lat_f},{lon_f}"})
                continue

            try:
                accuracy = round(float(data.get("accuracy")), 1)
            except (TypeError, ValueError):
                accuracy = None

            log.info("Location fix via %s: %.6f,%.6f", provider, lat_f, lon_f)
            return {
                "ok": True,
                "lat": round(lat_f, 6),
                "lon": round(lon_f, 6),
                "accuracy": accuracy,
                "provider": str(data.get("provider") or provider),
                "time": data.get("elapsedMs"),
                "maps_url": self.maps_url(lat_f, lon_f),
                "attempts": attempts,
            }

        error = "; ".join(f"{a['provider']}: {a['error']}" for a in attempts) or "no location provider"
        self.last_error = error
        log.warning("Location unavailable: %s", error)
        return {
            "ok": False,
            "lat": None,
            "lon": None,
            "accuracy": None,
            "provider": None,
            "maps_url": None,
            "attempts": attempts,
            "error": error,
        }

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #

    def build_summary(
        self,
        photo: Optional[Dict[str, Any]],
        location: Dict[str, Any],
        when: datetime,
        photo_attempted: bool = True,
    ) -> str:
        """Bengali sentence(s) that read well aloud and as a Telegram caption."""
        photo_ok = bool(photo and photo.get("ok"))
        location_ok = bool(location and location.get("ok"))

        if photo_ok:
            camera = photo.get("camera")
            where = "সামনের" if camera == FRONT_CAMERA else "পেছনের" if camera == REAR_CAMERA else ""
            parts = [f"সতর্কতা! কেউ ফোন হাতড়াচ্ছে। {where} ক্যামেরায় ছবি তোলা হয়েছে।".strip()]
        elif not photo_attempted:
            parts = ["সতর্কতা! কেউ ফোন হাতড়াচ্ছে। ছবি তোলার অনুরোধ ছিল না।"]
        else:
            parts = ["সতর্কতা! কেউ ফোন হাতড়াচ্ছে, কিন্তু ছবি তোলা যায়নি।"]

        if location_ok:
            parts.append(
                f"অবস্থান: অক্ষাংশ {_bn_number(location['lat'])}, "
                f"দ্রাঘিমাংশ {_bn_number(location['lon'])}।"
            )
            accuracy = location.get("accuracy")
            if accuracy:
                parts.append(f"নির্ভুলতা প্রায় {to_bengali_digits(int(accuracy))} মিটার।")
            parts.append("গুগল ম্যাপের লিংক পাঠাচ্ছি।")
        else:
            parts.append("অবস্থান পাওয়া যায়নি, লোকেশন চালু আছে কিনা দেখুন।")

        parts.append(f"সময় {to_bengali_digits(when.strftime('%H:%M'))}।")
        return " ".join(parts)

    # ------------------------------------------------------------------ #
    # The action
    # ------------------------------------------------------------------ #

    def intruder_check(
        self,
        capture: bool = True,
        providers: Sequence[str] = DEFAULT_PROVIDERS,
    ) -> Dict[str, Any]:
        """Full intruder package: photo + GPS + Maps link + Bengali summary.

        ``capture=False`` (brain sends it when the user only asked "কে আছে
        দেখো") still resolves the location. Never raises.
        """
        when = self._now()
        camera_attempts: list[Dict[str, Any]] = []
        photo: Optional[Dict[str, Any]] = None

        if capture:
            for camera_id in self.cameras:
                attempt = self.capture_photo(camera_id, when=when)
                camera_attempts.append(
                    {"camera": camera_id, "ok": attempt["ok"], "error": attempt.get("error")}
                )
                if attempt["ok"]:
                    photo = attempt
                    break

        location = self.locate(providers)
        summary = self.build_summary(photo, location, when, photo_attempted=bool(capture))
        photo_ok = bool(photo and photo.get("ok"))
        location_ok = bool(location.get("ok"))

        result: Dict[str, Any] = {
            "ok": photo_ok or location_ok,
            "action": "intruder_check",
            "timestamp": when.isoformat(timespec="seconds"),
            "epoch": int(when.timestamp()),
            "photo_path": photo.get("path") if photo_ok else None,
            "camera": photo.get("camera") if photo_ok else None,
            "photo_attempted": bool(capture),
            "lat": location.get("lat"),
            "lon": location.get("lon"),
            "accuracy": location.get("accuracy"),
            "provider": location.get("provider"),
            "maps_url": location.get("maps_url"),
            "location_ok": location_ok,
            "summary_bn": summary,
            "attempts": {"cameras": camera_attempts, "providers": location.get("attempts", [])},
        }

        if not result["ok"]:
            self.last_error = "intruder check captured neither a photo nor a location"
            result["error"] = self.last_error
            log.error(self.last_error)
        else:
            self.last_error = None

        self.last_result = result
        self.history.append(result)
        del self.history[:-HISTORY_LIMIT]
        log.info(
            "Intruder check: photo=%s location=%s summary=%d chars",
            "yes" if photo_ok else "no",
            "yes" if location_ok else "no",
            len(summary),
        )
        return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    args = sys.argv[1:]
    cameras = list(DEFAULT_CAMERAS)
    if "--camera" in args:
        idx = args.index("--camera")
        try:
            cameras = [int(args[idx + 1])]
        except (IndexError, ValueError):
            print("usage: python security_guard.py [--no-photo] [--camera 0|1]")
            raise SystemExit(2)

    print("termux-api:", {name: bool(which_binary(name)) for name in ("termux-camera-photo", "termux-location")})
    guard = SecurityGuard(cameras=cameras)
    output = guard.intruder_check(capture="--no-photo" not in args)
    print(json.dumps(output, ensure_ascii=False, indent=2))
