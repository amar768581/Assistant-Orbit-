"""
pc_automation.py — ORBIT's hands on a Windows PC.

This module replaces the Android-only device actions with things a desktop
actually has: the default browser, Explorer folders, installed applications and
real-time system health from :mod:`psutil`.

    from pc_automation import PcAutomation
    pc = PcAutomation()
    pc.open_website("https://github.com")
    pc.open_folder("~/Downloads")
    pc.open_app("notepad")
    pc.system_health()      # cpu / RAM / disk / battery, Bengali summary

Rules (same as the rest of ORBIT):

* never raise — every failure is a ``{"ok": False, "error": ...}`` dict,
* validate and expand user paths before touching the OS,
* the financial guardrail applies to URLs and application names too
  (``security_policy.financial_app_block``), so a bank app can never launch.

The OS opener, browser opener and psutil module are injectable, which keeps the
whole module testable on Linux CI without opening a single window.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from security_policy import BANKING_REFUSAL_BN, financial_app_block, is_financial_target

log = logging.getLogger("orbit.pc")

BN_DIGITS = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")

# Friendly (including Bengali) names -> executables registered on Windows.
APP_ALIASES: Dict[str, str] = {
    "notepad": "notepad.exe",
    "নোটপ্যাড": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "ক্যালকুলেটর": "calc.exe",
    "paint": "mspaint.exe",
    "পেইন্ট": "mspaint.exe",
    "explorer": "explorer.exe",
    "ফাইল": "explorer.exe",
    "files": "explorer.exe",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "powershell": "powershell.exe",
    "terminal": "wt.exe",
    "settings": "ms-settings:",
    "সেটিংস": "ms-settings:",
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
    "firefox": "firefox.exe",
    "code": "code.cmd",
    "vscode": "code.cmd",
    "vs code": "code.cmd",
    "word": "winword.exe",
    "excel": "excel.exe",
    "powerpoint": "powerpnt.exe",
    "vlc": "vlc.exe",
    "spotify": "spotify.exe",
    "whatsapp": "whatsapp.exe",
    "telegram": "telegram.exe",
}


def _bn(value: Any) -> str:
    """123 -> '১২৩' for spoken/printed Bengali numbers."""
    return str(value).translate(BN_DIGITS)


def _gb(value: float) -> str:
    return f"{value:.1f}"


class PcAutomation:
    """Browser, folders, applications and live PC health."""

    def __init__(
        self,
        settings: Optional[Any] = None,
        opener: Optional[Callable[[str], Any]] = None,
        browser: Optional[Callable[[str], Any]] = None,
        psutil_module: Any = None,
        platform_name: Optional[str] = None,
    ) -> None:
        self.settings = settings
        self._opener = opener
        self._browser = browser
        self._psutil = psutil_module
        self.platform_name = platform_name or platform.system()
        self.last_error: Optional[str] = None

    # -- plumbing ---------------------------------------------------------- #

    @property
    def is_windows(self) -> bool:
        return str(self.platform_name).lower().startswith("win")

    def _psutil_module(self) -> Any:
        module = self._psutil
        if module is None:
            try:
                import psutil  # type: ignore

                module = psutil
            except Exception as exc:  # pragma: no cover - dependency missing
                self.last_error = f"psutil নেই — pip install psutil ({exc})"
                return None
            self._psutil = module
        return module

    def _default_open(self, target: str) -> bool:
        """Open a file/folder/app with the OS handler (async, no window wait)."""
        try:
            if self.is_windows:
                os.startfile(target)  # type: ignore[attr-defined]
                return True
            command = ["open", target] if sys.platform == "darwin" else ["xdg-open", target]
            subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception as exc:
            self.last_error = f"open failed: {exc}"
            log.warning("Could not open %r: %s", target, exc)
            return False

    def _open(self, target: str) -> bool:
        opener = self._opener or self._default_open
        try:
            result = opener(target)
        except Exception as exc:
            self.last_error = f"open failed: {exc}"
            log.warning("Opener raised for %r: %s", target, exc)
            return False
        return True if result is None else bool(result)

    @staticmethod
    def _fail(message: str, **extra: Any) -> Dict[str, Any]:
        return {"ok": False, "error": message, **extra}

    # -- browser ----------------------------------------------------------- #

    def open_website(self, url: str) -> Dict[str, Any]:
        """Open ``url`` in the default browser (https assumed when missing)."""
        raw = str(url or "").strip()
        if not raw:
            return self._fail("empty url", message_bn="বস, কোন ওয়েবসাইট খুলব বললেন না।")

        target = raw if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw) else f"https://{raw}"
        if not re.match(r"^https?://", target, re.IGNORECASE):
            return self._fail(
                f"only http/https urls are allowed: {raw!r}",
                message_bn="বস, শুধু ওয়েবসাইটের লিংক খুলতে পারি।",
                url=raw,
            )

        if is_financial_target(target):
            log.warning("Blocked financial website: %s", target)
            return {
                "ok": False,
                "error": "banking_app_blocked",
                "url": target,
                "reply_bn": BANKING_REFUSAL_BN,
                "message_bn": BANKING_REFUSAL_BN,
            }

        browser = self._browser or (lambda link: webbrowser.open(link, new=2))
        try:
            opened = browser(target)
        except Exception as exc:
            self.last_error = f"browser failed: {exc}"
            log.warning("Browser failed for %s: %s", target, exc)
            return self._fail(self.last_error, url=target)
        if opened is False:
            self.last_error = "browser refused to open the url"
            return self._fail(self.last_error, url=target)

        log.info("Opened website %s", target)
        return {
            "ok": True,
            "url": target,
            "message_bn": f"বস, {target} ব্রাউজারে খুলছি।",
        }

    # -- folders ----------------------------------------------------------- #

    @staticmethod
    def expand_path(path: Any) -> Path:
        """Expand ``~``, ``%USERPROFILE%`` and ``$HOME`` without raising."""
        raw = str(path or "").strip().strip('"').strip("'")
        if not raw:
            return Path("")
        if raw == "~" or raw.startswith(("~/", "~\\")):
            raw = str(Path.home()) + raw[1:]
        raw = os.path.expandvars(os.path.expanduser(raw))
        return Path(raw)

    def open_folder(self, path: str) -> Dict[str, Any]:
        """Open an existing local folder in Explorer/Finder/File Manager."""
        folder = self.expand_path(path)
        if not str(folder) or str(folder) == ".":
            return self._fail("empty folder path", message_bn="বস, কোন ফোল্ডার খুলব বললেন না।")
        if not folder.exists():
            return self._fail(
                f"folder not found: {folder}",
                message_bn=f"বস, {folder} ফোল্ডারটা পাওয়া যায়নি।",
                path=str(folder),
            )
        if not folder.is_dir():
            return self._fail(
                f"not a folder: {folder}",
                message_bn=f"বস, {folder} আসলে ফোল্ডার নয়।",
                path=str(folder),
            )

        resolved = folder.resolve()
        if not self._open(str(resolved)):
            return self._fail(
                self.last_error or "could not open folder",
                message_bn="বস, ফোল্ডারটা খোলা গেল না।",
                path=str(resolved),
            )
        log.info("Opened folder %s", resolved)
        return {
            "ok": True,
            "path": str(resolved),
            "message_bn": f"বস, {resolved.name or resolved} ফোল্ডার খুলছি।",
        }

    # -- applications ------------------------------------------------------ #

    def open_app(self, name: str) -> Dict[str, Any]:
        """Launch a desktop application by friendly name or executable/path."""
        raw = str(name or "").strip()
        if not raw:
            return self._fail("empty app name", message_bn="বস, কোন অ্যাপ খুলব বললেন না।")

        if is_financial_target(raw):
            log.warning("Blocked financial desktop app: %s", raw)
            return financial_app_block(raw)

        lowered = raw.lower().strip()
        candidate = APP_ALIASES.get(lowered, raw)
        target = candidate

        # A bare name such as "notepad" is resolved through PATH; paths and
        # registered names are handed to the shell unchanged.
        looks_like_path = any(sep in candidate for sep in ("\\", "/")) or Path(candidate).suffix
        if not looks_like_path and not candidate.endswith(":"):
            found = shutil.which(candidate) or shutil.which(f"{candidate}.exe")
            if found:
                target = found
            elif self.is_windows and not candidate.lower().endswith(".exe"):
                target = f"{candidate}.exe"

        if not self._open(target):
            return self._fail(
                self.last_error or f"could not launch {raw}",
                message_bn=f"বস, {raw} অ্যাপটা খোলা গেল না।",
                app=raw,
            )

        log.info("Launched app %s (%s)", raw, target)
        return {
            "ok": True,
            "app": raw,
            "target": target,
            "name_bn": raw,
            "message_bn": f"বস, {raw} খুলছি।",
        }

    # -- health ------------------------------------------------------------ #

    def _battery(self) -> Dict[str, Any]:
        module = self._psutil_module()
        if module is None:
            return {"percent": None, "plugged": None, "seconds_left": None}
        try:
            battery = module.sensors_battery()
        except Exception:
            battery = None
        if battery is None:
            return {"percent": None, "plugged": None, "seconds_left": None}
        percent = getattr(battery, "percent", None)
        plugged = getattr(battery, "power_plugged", None)
        seconds = getattr(battery, "secsleft", None)
        try:
            seconds = int(seconds) if seconds is not None and int(seconds) >= 0 else None
        except (TypeError, ValueError):
            seconds = None
        return {"percent": percent, "plugged": plugged, "seconds_left": seconds}

    def system_stats(self) -> Dict[str, Any]:
        """Raw CPU/RAM/disk/battery numbers (all JSON-safe)."""
        module = self._psutil_module()
        if module is None:
            return self._fail(self.last_error or "psutil unavailable")

        try:
            cpu_percent = float(module.cpu_percent(interval=0.2))
            cpu_count = int(module.cpu_count() or 0)
            memory = module.virtual_memory()
            anchor = Path.home().anchor or ("C:\\" if self.is_windows else "/")
            disk = module.disk_usage(anchor)
            boot = float(module.boot_time())
        except Exception as exc:
            self.last_error = f"system stats failed: {exc}"
            log.warning(self.last_error)
            return self._fail(self.last_error)

        try:
            boot = float(module.boot_time())
        except Exception:
            boot = 0.0

        battery = self._battery()
        return {
            "ok": True,
            "cpu_percent": round(cpu_percent, 1),
            "cpu_count": cpu_count,
            "ram_total_gb": round(memory.total / 1024**3, 1),
            "ram_used_gb": round(memory.used / 1024**3, 1),
            "ram_available_gb": round(memory.available / 1024**3, 1),
            "ram_percent": round(float(memory.percent), 1),
            "disk_total_gb": round(disk.total / 1024**3, 1),
            "disk_used_gb": round(disk.used / 1024**3, 1),
            "disk_free_gb": round(disk.free / 1024**3, 1),
            "disk_percent": round(float(disk.percent), 1),
            "uptime_hours": round(max(0.0, (time_now() - boot) / 3600.0), 1) if boot else None,
            "battery_percent": battery["percent"],
            "battery_plugged": battery["plugged"],
            "battery_seconds_left": battery["seconds_left"],
        }

    def system_health(self, top: int = 3) -> Dict[str, Any]:
        """Human-ready PC health report with a Bengali spoken summary."""
        stats = self.system_stats()
        if not stats.get("ok"):
            return stats

        module = self._psutil_module()
        processes = []
        if module is not None:
            try:
                rows = sorted(
                    (
                        (proc.info.get("name") or "?", proc.info.get("memory_info"))
                        for proc in module.process_iter(["name", "memory_info"])
                        if proc.info.get("memory_info") is not None
                    ),
                    key=lambda item: item[1].rss,
                    reverse=True,
                )
                processes = [
                    {"name": name, "ram_mb": round(info.rss / 1024**2, 1)}
                    for name, info in rows[: max(1, int(top))]
                ]
            except Exception as exc:  # pragma: no cover - platform differences
                log.debug("process list unavailable: %s", exc)

        battery = stats.get("battery_percent")
        if battery is None:
            battery_bn = "ব্যাটারি নেই (ডেস্কটপ)"
        else:
            state_bn = "চার্জে" if stats.get("battery_plugged") else "চার্জে নেই"
            battery_bn = f"ব্যাটারি {_bn(battery)} শতাংশ, {state_bn}"

        summary = (
            f"বস, সিপিইউ {_bn(stats['cpu_percent'])} শতাংশ, "
            f"র‍্যাম {_bn(_gb(stats['ram_used_gb']))} / {_bn(_gb(stats['ram_total_gb']))} জিবি "
            f"({_bn(stats['ram_percent'])}%), "
            f"ডিস্কে {_bn(_gb(stats['disk_free_gb']))} জিবি খালি। {battery_bn}।"
        )
        stats.update(
            {
                "top_processes": processes,
                "summary_bn": summary,
                "message_bn": summary,
                "reply_bn": summary,
            }
        )
        return stats


def time_now() -> float:
    """Indirection so tests can freeze uptime without touching the clock."""
    import time

    return time.time()
