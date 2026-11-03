"""
voice_engine.py — ORBIT's mouth and ears inside Termux.

TTS (natural Bengali):
    edge-tts (bn-IN-BashkarNeural / bn-IN-TanishaaNeural) -> mp3
    playback chain:  mpv  ->  termux-media-player  ->  termux-tts-speak

STT:
    termux-microphone-record  ->  m4a  ->  Groq Whisper (whisper-large-v3-turbo)

Every device binary is optional: missing tools are logged and ORBIT keeps
running (important for desktop tests and for a half-installed Termux setup).

    from voice_engine import VoiceEngine
    v = VoiceEngine()
    v.speak("নমস্কার, আমি অরবিট।")
    text = v.listen(seconds=6)      # "" when the mic is unavailable
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

from config import BASE_DIR, Settings, settings as default_settings

log = logging.getLogger("orbit.voice")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

VOICE_MALE = "bn-IN-BashkarNeural"
VOICE_FEMALE = "bn-IN-TanishaaNeural"
DEFAULT_VOICE = VOICE_MALE

GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-large-v3-turbo"
WHISPER_LANGUAGE = "bn"  # ISO-639-1; Whisper still tolerates English in the audio

AUDIO_DIR = BASE_DIR / "audio" / "tts"
RECORD_DIR = BASE_DIR / "audio" / "recordings"

MAX_RECORD_SECONDS = 30.0
SPEECH_TIMEOUT_PAD = 20.0  # extra seconds allowed for mpv playback

MIME_BY_SUFFIX = {
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".flac": "audio/flac",
    ".3gp": "audio/3gpp",
    ".amr": "audio/amr",
}


class VoiceError(RuntimeError):
    """Raised when a voice operation cannot be completed."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _run_coroutine(coro: Any) -> Any:
    """Run an awaitable from sync code, even if a loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: Dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised in caller thread
            box["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _binary(name: str) -> Optional[str]:
    """Return the absolute path of a Termux binary, if present."""
    return shutil.which(name)


def _run(cmd: List[str], timeout: float) -> Tuple[int, str]:
    """Run a command capturing output; never raises."""
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = (completed.stdout or "").strip()
        err = (completed.stderr or "").strip()
        return completed.returncode, (output + ("\n" + err if err else "")).strip()
    except subprocess.TimeoutExpired:
        log.warning("Command timed out after %.0fs: %s", timeout, cmd[0])
        return 124, "timeout"
    except Exception as exc:  # binary vanished, permission problem, ...
        log.warning("Command failed (%s): %s", cmd[0], exc)
        return 1, str(exc)


def _prune(directory: Path, keep: int = 20) -> None:
    """Keep the audio cache small — Termux storage fills up fast."""
    try:
        files = sorted(
            (p for p in directory.glob("*") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in files[keep:]:
            stale.unlink(missing_ok=True)
    except Exception as exc:  # pragma: no cover - filesystem oddities
        log.debug("Could not prune %s: %s", directory, exc)


# --------------------------------------------------------------------------- #
# VoiceEngine
# --------------------------------------------------------------------------- #

class VoiceEngine:
    """Bengali TTS + Groq Whisper STT for Termux."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        voice: str = DEFAULT_VOICE,
        language: Optional[str] = None,
        session: Any = None,
        timeout: float = 60.0,
        audio_dir: Path = AUDIO_DIR,
        record_dir: Path = RECORD_DIR,
    ) -> None:
        self.settings = settings or default_settings
        self.voice = voice or DEFAULT_VOICE
        if language and language.strip().lower() == "auto":
            self.language = "auto"  # let Whisper auto-detect (mixed bn/en)
        elif language:
            self.language = self._whisper_language(language)
        else:
            self.language = self._whisper_language(self.settings.language)
        self.timeout = timeout
        self._session = session
        self.audio_dir = Path(audio_dir)
        self.record_dir = Path(record_dir)

        # Observability for the caller / Telegram bridge.
        self.last_tts_engine: Optional[str] = None
        self.last_audio_path: Optional[Path] = None
        self.last_error: Optional[str] = None

        for directory in (self.audio_dir, self.record_dir):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except Exception as exc:  # pragma: no cover
                log.warning("Cannot create %s: %s", directory, exc)

    # -- capability probing -------------------------------------------------

    @staticmethod
    def _whisper_language(language: str) -> str:
        """'bn-IN' -> 'bn'; anything unrecognised falls back to Bengali."""
        code = (language or "").strip().lower().replace("_", "-")
        if not code:
            return WHISPER_LANGUAGE
        primary = code.split("-")[0]
        return primary if len(primary) == 2 else WHISPER_LANGUAGE

    @property
    def can_play(self) -> bool:
        return any(_binary(name) for name in ("mpv", "termux-media-player", "termux-tts-speak"))

    @property
    def can_record(self) -> bool:
        return _binary("termux-microphone-record") is not None

    def diagnostics(self) -> Dict[str, Optional[str]]:
        """Which pieces of the voice stack this device actually has."""
        return {
            "edge-tts": "python-package",
            "mpv": _binary("mpv"),
            "termux-media-player": _binary("termux-media-player"),
            "termux-tts-speak": _binary("termux-tts-speak"),
            "termux-microphone-record": _binary("termux-microphone-record"),
            "groq_key": "<set>" if self.settings.groq_api_key else None,
        }

    # ------------------------------------------------------------------ #
    # TTS
    # ------------------------------------------------------------------ #

    def speak(
        self,
        text: str,
        voice: Optional[str] = None,
        rate: str = "+0%",
        volume: str = "+0%",
        fallback_to_termux: bool = True,
    ) -> bool:
        """Say ``text`` out loud. Returns True if something was actually played.

        Order: edge-tts MP3 + mpv -> edge-tts + termux-media-player ->
        termux-tts-speak. Never raises; failures land in ``self.last_error``.
        """
        clean = (text or "").strip()
        if not clean:
            log.debug("speak() called with empty text")
            return False

        self.last_error = None
        self.last_tts_engine = None

        # 1) Natural Bengali neural voice.
        audio = self.synthesize(clean, voice=voice, rate=rate, volume=volume)
        if audio is not None:
            self.last_audio_path = audio
            if self._play_file(audio):
                return True
            log.warning("Could not play %s with any local player", audio)

        # 2) Android's built-in engine (robotic but always there).
        if fallback_to_termux:
            if self._speak_termux(clean):
                return True

        log.error("TTS failed for %d characters: %s", len(clean), self.last_error)
        return False

    def synthesize(
        self,
        text: str,
        voice: Optional[str] = None,
        rate: str = "+0%",
        volume: str = "+0%",
    ) -> Optional[Path]:
        """Render ``text`` to an MP3 with edge-tts. Returns the path or None."""
        try:
            import edge_tts  # imported lazily: optional on desktop
        except ImportError:
            self.last_error = "edge-tts not installed (pip install edge-tts)"
            log.warning("%s — falling back to termux-tts-speak", self.last_error)
            return None

        selected = voice or self.voice
        try:
            self.audio_dir.mkdir(parents=True, exist_ok=True)
            target = self.audio_dir / f"orbit_{int(time.time() * 1000)}.mp3"
            communicate = edge_tts.Communicate(
                text,
                selected,
                rate=rate,
                volume=volume,
            )
            _run_coroutine(communicate.save(str(target)))

            if not target.is_file() or target.stat().st_size < 512:
                self.last_error = "edge-tts produced an empty file"
                log.warning("%s (voice=%s)", self.last_error, selected)
                target.unlink(missing_ok=True)
                return None

            _prune(self.audio_dir)
            log.debug("edge-tts wrote %s (%d bytes)", target.name, target.stat().st_size)
            return target
        except Exception as exc:
            self.last_error = f"edge-tts failed: {exc}"
            log.warning("edge-tts synthesis failed (voice=%s): %s", selected, exc)
            return None

    def _play_file(self, path: Path) -> bool:
        """Play a local audio file, trying mpv then termux-media-player."""
        budget = min(180.0, 15.0 + path.stat().st_size / 4000.0) + SPEECH_TIMEOUT_PAD

        mpv = _binary("mpv")
        if mpv:
            code, output = _run(
                [mpv, "--no-video", "--really-quiet", "--no-terminal", str(path)],
                timeout=budget,
            )
            if code == 0:
                self.last_tts_engine = "mpv"
                log.info("Spoke via mpv (%s)", path.name)
                return True
            log.warning("mpv exited %s: %s", code, output[:200])
            self.last_error = f"mpv failed: {output[:200]}"

        player = _binary("termux-media-player")
        if player:
            code, output = _run([player, "play", str(path)], timeout=budget)
            if code == 0:
                self.last_tts_engine = "termux-media-player"
                log.info("Spoke via termux-media-player (%s)", path.name)
                return True
            log.warning("termux-media-player exited %s: %s", code, output[:200])
            self.last_error = f"termux-media-player failed: {output[:200]}"

        if not mpv and not player:
            self.last_error = "no audio player found (pkg install mpv / termux-api)"
        return False

    def _speak_termux(self, text: str) -> bool:
        binary = _binary("termux-tts-speak")
        if not binary:
            missing = "termux-tts-speak not found (pkg install termux-api)"
            self.last_error = f"{self.last_error}; {missing}" if self.last_error else missing
            return False
        code, output = _run(
            [binary, "-l", self.settings.language or "bn-IN", text],
            timeout=min(180.0, 10.0 + len(text) / 6.0),
        )
        if code == 0:
            self.last_tts_engine = "termux-tts-speak"
            log.info("Spoke via termux-tts-speak")
            return True
        log.warning("termux-tts-speak exited %s: %s", code, output[:200])
        self.last_error = f"termux-tts-speak failed: {output[:200]}"
        return False

    def stop_speaking(self) -> None:
        """Stop anything currently playing."""
        player = _binary("termux-media-player")
        if player:
            _run([player, "stop"], timeout=10)
        mpv = _binary("mpv")
        if mpv:
            # mpv is blocking per call, so a best-effort remote stop is enough.
            _run(["pkill", "-f", "mpv --no-video"], timeout=5)

    # ------------------------------------------------------------------ #
    # STT
    # ------------------------------------------------------------------ #

    def record(self, seconds: float = 6.0, path: Optional[Path] = None) -> Optional[Path]:
        """Record from the microphone with termux-microphone-record."""
        binary = _binary("termux-microphone-record")
        if not binary:
            self.last_error = "termux-microphone-record not found (pkg install termux-api)"
            log.warning(self.last_error)
            return None

        duration = max(1.0, min(float(seconds), MAX_RECORD_SECONDS))
        try:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            target = Path(path) if path else (
                self.record_dir / f"mic_{int(time.time() * 1000)}.m4a"
            )
            target.unlink(missing_ok=True)

            process = subprocess.Popen(
                [
                    binary,
                    "-f", str(target),
                    "-l", str(int(round(duration))),
                    "-e", "aac",
                    "-b", "128000",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            started = time.monotonic()
            deadline = started + duration + 8.0
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                ready = target.is_file() and target.stat().st_size > 1024
                if ready and time.monotonic() - started >= duration:
                    _run([binary, "-q"], timeout=5)  # graceful stop
                    break
                time.sleep(0.2)

            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()

            if not target.is_file() or target.stat().st_size < 1024:
                self.last_error = "recording produced no audio (mic permission?)"
                log.warning(self.last_error)
                return None

            _prune(self.record_dir)
            log.info("Recorded %ss -> %s (%d bytes)", int(duration), target.name, target.stat().st_size)
            return target
        except Exception as exc:
            self.last_error = f"recording failed: {exc}"
            log.warning("Recording failed: %s", exc)
            return None

    def transcribe(self, audio_path: Path, language: Optional[str] = None) -> str:
        """Send an audio file to Groq Whisper and return the text."""
        path = Path(audio_path)
        if not path.is_file():
            raise VoiceError(f"audio file not found: {path}")
        if not self.settings.groq_api_key:
            raise VoiceError("GROQ_API_KEY missing — cannot transcribe (see .env.example)")
        if requests is None and self._session is None:
            raise VoiceError("requests is not installed (pip install -r requirements.txt)")

        model = os.environ.get("WHISPER_MODEL", WHISPER_MODEL)
        lang = (language or self.language or WHISPER_LANGUAGE).lower()
        data = {
            "model": model,
            "response_format": "json",
            "temperature": "0",
        }
        if lang and lang != "auto":
            data["language"] = lang
        if "prompt" not in data:
            data["prompt"] = "বাংলা বা Banglish কথ্য ভাষা।"  # nudges Bengali output

        headers = {"Authorization": f"Bearer {self.settings.groq_api_key}"}
        mime = MIME_BY_SUFFIX.get(path.suffix.lower(), "audio/mp4")

        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                with path.open("rb") as handle:
                    files = {"file": (path.name, handle, mime)}
                    if self._session is not None:
                        response = self._session.post(
                            GROQ_TRANSCRIBE_URL,
                            headers=headers,
                            data=data,
                            files=files,
                            timeout=self.timeout,
                        )
                    else:
                        response = requests.post(
                            GROQ_TRANSCRIBE_URL,
                            headers=headers,
                            data=data,
                            files=files,
                            timeout=self.timeout,
                        )

                if response.status_code == 200:
                    text = (response.json().get("text") or "").strip()
                    log.info("Whisper transcribed %d chars", len(text))
                    return text

                if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    log.warning("Groq STT HTTP %s, retrying", response.status_code)
                    time.sleep(1.5 * (attempt + 1))
                    continue

                raise VoiceError(
                    f"Groq STT HTTP {response.status_code}: {(response.text or '')[:300]}"
                )
            except VoiceError:
                raise
            except Exception as exc:
                last_exc = exc
                log.warning("Groq STT request failed (attempt %d/3): %s", attempt + 1, exc)
                time.sleep(1.5 * (attempt + 1))

        raise VoiceError(f"Groq STT unreachable after retries: {last_exc}")

    def listen(
        self,
        seconds: float = 6.0,
        language: Optional[str] = None,
        keep_audio: bool = False,
    ) -> str:
        """Record + transcribe in one call. Returns "" on any failure."""
        self.last_error = None
        audio = self.record(seconds=seconds)
        if audio is None:
            return ""

        try:
            text = self.transcribe(audio, language=language)
            return text
        except VoiceError as exc:
            self.last_error = str(exc)
            log.error("Transcription failed: %s", exc)
            return ""
        finally:
            if not keep_audio:
                audio.unlink(missing_ok=True)

    def listen_and_confirm(self, seconds: float = 6.0) -> str:
        """Record, transcribe, and echo the text back aloud for confirmation."""
        text = self.listen(seconds=seconds)
        if text:
            self.speak(text)
        return text


if __name__ == "__main__":  # python voice_engine.py [--listen]
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    engine = VoiceEngine()

    print("voice stack:", engine.diagnostics())
    if "--listen" in sys.argv:
        print("listening for 6 seconds...")
        heard = engine.listen(seconds=6, keep_audio=True)
        print("heard:", heard or f"<nothing — {engine.last_error}>")
    else:
        ok = engine.speak("নমস্কার! আমি অরবিট, আপনার ব্যক্তিগত সহকারী।")
        print("spoken:", ok, "| engine:", engine.last_tts_engine, "| error:", engine.last_error)
