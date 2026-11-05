"""
comm_manager.py — ORBIT's outgoing calls and SMS.

Both actions need one thing first: a real phone number. ``make_call`` and
``send_sms`` therefore delegate name/number resolution to
:meth:`system_control.SystemControl.resolve_contact` (spoken digits win, then
the address book with Bengali/Banglish fuzzy matching) and only then touch
Termux:API.

    from comm_manager import CommManager
    comm = CommManager()
    comm.make_call(contact="রহিম")
    comm.send_sms(number="01712345678", message="আসছি, অপেক্ষা করুন")

Every method returns ``{"ok": bool, ...}`` and never raises — a missing
``termux-telephony-call`` binary becomes ``{"ok": False, "error": ...}``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence

from config import settings as default_settings
from system_control import CommandResult, SystemControl, normalise_number, run_command

log = logging.getLogger("orbit.comm")

CALL_TIMEOUT = 20.0
SMS_TIMEOUT = 30.0


class CommManager:
    """Places calls and sends SMS through Termux:API."""

    def __init__(
        self,
        control: Optional[SystemControl] = None,
        runner: Any = run_command,
        settings: Optional[Any] = None,
        timeout: float = CALL_TIMEOUT,
    ) -> None:
        self.settings = settings or default_settings
        self.control = control or SystemControl(settings=self.settings)
        self._runner = runner
        self.timeout = timeout
        self.last_error: Optional[str] = None

    # -- plumbing ---------------------------------------------------------- #

    def _run(self, cmd: Sequence[str], timeout: Optional[float] = None) -> CommandResult:
        try:
            return self._runner(list(cmd), timeout or self.timeout)
        except TypeError:  # a fake runner that only takes the command
            return self._runner(list(cmd))

    @staticmethod
    def _fail(error: str, action: str, **extra: Any) -> Dict[str, Any]:
        return {"ok": False, "action": action, "error": error, **extra}

    def _resolve(
        self,
        action: str,
        contact: Optional[str],
        number: Optional[str],
    ) -> Dict[str, Any]:
        """Normalise the recipient; returns an error dict when impossible."""
        spoken = (contact or "").strip()
        digits = normalise_number(number)

        resolved = self.control.resolve_contact(contact=spoken or None, number=number)
        if not resolved.get("ok"):
            query = spoken or digits or "?"
            message = resolved.get("error") or f"contact not found: {query}"
            return self._fail(message, action, query=query)

        target = normalise_number(resolved.get("number"))
        if not target:
            return self._fail(
                f"no phone number found for {resolved.get('name') or spoken or digits}",
                action,
                name=resolved.get("name"),
            )

        name = (resolved.get("name") or "").strip() or target
        return {
            "ok": True,
            "action": action,
            "name": name,
            "number": target,
            "source": resolved.get("source", "contacts"),
        }

    @staticmethod
    def _recipient_bn(name: str, number: str) -> str:
        """'রহিম' for contacts, the number itself when that is all we have."""
        return name if name and name != number else number

    # ------------------------------------------------------------------ #
    # Call
    # ------------------------------------------------------------------ #

    def make_call(
        self,
        contact: Optional[str] = None,
        number: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolve the recipient and dial (`termux-telephony-call <number>`)."""
        recipient = self._resolve("call", contact, number)
        if not recipient["ok"]:
            self.last_error = recipient["error"]
            log.warning("Call not placed: %s", self.last_error)
            return recipient

        target = recipient["number"]
        result = self._run(["termux-telephony-call", target], timeout=self.timeout)
        if not result.ok:
            self.last_error = result.describe()
            log.warning("Call to %s failed: %s", target, self.last_error)
            return self._fail(
                self.last_error,
                "call",
                name=recipient["name"],
                number=target,
                message_bn=f"{self._recipient_bn(recipient['name'], target)}কে কল দেওয়া গেল না।",
            )

        self.last_error = None
        log.info("Calling %s (%s)", recipient["name"], target)
        return {
            "ok": True,
            "action": "call",
            "name": recipient["name"],
            "number": target,
            "source": recipient["source"],
            "detail": f"termux-telephony-call {target}",
            "message_bn": f"{self._recipient_bn(recipient['name'], target)}কে কল দিচ্ছি।",
        }

    # ------------------------------------------------------------------ #
    # SMS
    # ------------------------------------------------------------------ #

    def send_sms(
        self,
        contact: Optional[str] = None,
        number: Optional[str] = None,
        message: str = "",
    ) -> Dict[str, Any]:
        """Resolve the recipient and send (`termux-sms-send -n <number> <text>`)."""
        body = str(message or "").strip()
        if not body:
            self.last_error = "empty SMS body"
            return self._fail("মেসেজে কী লিখব বলবেন?", "sms", name=contact or number)

        recipient = self._resolve("sms", contact, number)
        if not recipient["ok"]:
            self.last_error = recipient["error"]
            log.warning("SMS not sent: %s", self.last_error)
            return recipient

        target = recipient["number"]
        result = self._run(["termux-sms-send", "-n", target, body], timeout=self.timeout)
        if not result.ok:
            self.last_error = result.describe()
            log.warning("SMS to %s failed: %s", target, self.last_error)
            return self._fail(
                self.last_error,
                "sms",
                name=recipient["name"],
                number=target,
                message=body,
                message_bn=f"{self._recipient_bn(recipient['name'], target)}কে মেসেজ পাঠানো গেল না।",
            )

        self.last_error = None
        log.info("SMS sent to %s (%s), %d chars", recipient["name"], target, len(body))
        return {
            "ok": True,
            "action": "sms",
            "name": recipient["name"],
            "number": target,
            "message": body,
            "source": recipient["source"],
            "detail": f"termux-sms-send -n {target}",
            "message_bn": f"{self._recipient_bn(recipient['name'], target)}কে মেসেজ পাঠাচ্ছি।",
        }


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    manager = CommManager()

    if len(sys.argv) < 2:
        print("usage: python comm_manager.py call <name|number>")
        print("       python comm_manager.py sms <name|number> <message>")
    elif sys.argv[1] == "call":
        print(json.dumps(manager.make_call(contact=" ".join(sys.argv[2:])), ensure_ascii=False, indent=2))
    elif sys.argv[1] == "sms" and len(sys.argv) > 3:
        print(json.dumps(
            manager.send_sms(contact=sys.argv[2], message=" ".join(sys.argv[3:])),
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print("usage: python comm_manager.py sms <name|number> <message>")
