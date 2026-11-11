"""
voice_engine.py — ORBIT's mouth and ears on the PC.

TTS (natural Bengali):
    edge-tts (bn-IN-BashkarNeural / bn-IN-TanishaaNeural) -> temporary mp3
    playback with pygame.mixer (no external window / media player popup)
    fallback: Windows SAPI through PowerShell
    the temporary mp3 is always deleted after speaking

STT:
    sounddevice + soundfile (optional) -> wav -> Groq Whisper

Every dependency is optional and imported lazily: a missing package becomes a
clean ``last_error`` and ORBIT keeps running.

    from voice_engine import VoiceEngine
    v = VoiceEngine()
    v.speak("নমস্কার, আমি অরবিট।")
    text = v.listen(seconds=6)
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

from config import Settings, settings as default_settings

log = logging.getLogger("orbit.voice")

VOICE_MALE = "bn-IN-BashkarNeural"
VOICE_FEMALE = "bn-IN-TanishaaNeural"
DEFAULT_VOICE = VOICE_MALE

GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-large-v3-turbo"
WHISPER_LANGUAGE = "bn"

# Temporary TTS files live outside the repo and are deleted after playback.
AUDIO_DIR = Path(tempfile.gettempdir()) / "orbit_tts"
RECORD_DIR = Path.home() / ".orbit" / "recordings"

MAX_RECORD_SECONDS = 30.0
PLAYBACK_PAD = 20.0

MIME_BY_SUFFIX = {
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".flac": "audio/flac",
}


class VoiceError(RuntimeError):
    """Raised when a voice operation cannot be completed."""


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
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _module_available(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _run(cmd: list, timeout: float) -> Tuple[int, str]:
    """Run a helper command capturing output; never raises."""
    import subprocess

    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        output = (completed.stdout or "").strip()
        err = (completed.stderr or "").strip()
        return completed.returncode, (output + ("\n" + err if err else "")).strip()
    except subprocess.TimeoutExpired:
        log.warning("Command timed out after %.0fs: %s", timeout, cmd[0])
        return 124, "timeout"
    except Exception as exc:
        log.warning("Command failed (%s): %s", cmd[0], exc)
        return 1, str(exc)


def _prune(directory: Path, keep: int = 20) -> None:
    try:
        files = sorted(
            (p for p in directory.glob("*") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in files[keep:]:
            stale.unlink(missing_ok=True)
    except Exception as exc:  # pragma: no cover
        log.debug("Could not prune %s: %s", directory, exc)


class VoiceEngine:
    """Bengali TTS + Groq Whisper STT for a Windows PC."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        voice: str = DEFAULT_VOICE,
        language: Optional[str] = None,
        session: Any = None,
        timeout: float = 60.0,
        audio_dir: Path = AUDIO_DIR,
        record_dir: Path = RECORD_DIR,
        mixer: Any = None,
        sapi: Optional[Callable[[str], bool]] = None,
        recorder: Optional[Callable[..., Optional[Path]]] = None,
    ) -> None:
        self.settings = settings or default_settings
        self.voice = voice or DEFAULT_VOICE
        if language and language.strip().lower() == "auto":
            self.language = "auto"
        elif language:
            self.language = self._whisper_language(language)
        else:
            self.language = self._whisper_language(getattr(self.settings, "language", "bn-IN"))
        self.timeout = timeout
        self._session = session
        self.audio_dir = Path(audio_dir)
        self.record_dir = Path(record_dir)
        self._mixer = mixer
        self._sapi = sapi
        self._recorder = recorder

        self.last_tts_engine: Optional[str] = None
        self.last_audio_path: Optional[Path] = None
        self.last_error: Optional[str] = None

        for directory in (self.audio_dir, self.record_dir):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except Exception as exc:  # pragma: no cover
                log.warning("Cannot create %s: %s", directory, exc)

    # -- capability probing ------------------------------------------------- #

    @staticmethod
    def _whisper_language(language: str) -> str:
        code = (language or "").strip().lower().replace("_", "-")
        if not code:
            return WHISPER_LANGUAGE
        primary = code.split("-")[0]
        return primary if len(primary) == 2 else WHISPER_LANGUAGE

    @property
    def can_play(self) -> bool:
        return self._mixer is not None or _module_available("pygame")

    @property
    def can_record(self) -> bool:
        return self._recorder is not None or (
            _module_available("sounddevice") and _module_available("soundfile")
        )

    def diagnostics(self) -> Dict[str, Optional[str]]:
        return {
            "edge-tts": "installed" if _module_available("edge_tts") else None,
            "pygame": "installed" if self.can_play else None,
            "sapi": "Windows" if os.name == "nt" else None,
            "sounddevice": "installed" if _module_available("sounddevice") else None,
            "groq_key": "<set>" if getattr(self.settings, "groq_api_key", "") else None,
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
        fallback_to_sapi: bool = True,
    ) -> bool:
        """Say ``text`` out loud. Returns True if audio actually played.

        The synthesized file is always removed, even when playback fails.
        """
        clean = (text or "").strip()
        if not clean:
            log.debug("speak() called with empty text")
            return False

        self.last_error = None
        self.last_tts_engine = None

        audio = self.synthesize(clean, voice=voice, rate=rate, volume=volume)
        if audio is not None:
            self.last_audio_path = audio
            try:
                if self._play_file(audio):
                    return True
                log.warning("Could not play %s", audio)
            finally:
                try:
                    audio.unlink(missing_ok=True)
                except Exception as exc:  # pragma: no cover
                    log.debug("Could not delete %s: %s", audio, exc)

        if fallback_to_sapi and self._speak_sapi(clean):
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
        """Render ``text`` to a temporary MP3 with edge-tts."""
        try:
            import edge_tts  # imported lazily: optional dependency
        except ImportError:
            self.last_error = "edge-tts not installed (pip install edge-tts)"
            log.warning(self.last_error)
            return None

        selected = voice or self.voice
        try:
            self.audio_dir.mkdir(parents=True, exist_ok=True)
            target = self.audio_dir / f"orbit_{int(time.time() * 1000)}.mp3"
            communicate = edge_tts.Communicate(text, selected, rate=rate, volume=volume)
            _run_coroutine(communicate.save(str(target)))

            if not target.is_file() or target.stat().st_size < 512:
                self.last_error = "edge-tts produced an empty file"
                target.unlink(missing_ok=True)
                return None

            _prune(self.audio_dir)
            return target
        except Exception as exc:
            self.last_error = f"edge-tts failed: {exc}"
            log.warning("edge-tts synthesis failed (voice=%s): %s", selected, exc)
            return None

    def _play_file(self, path: Path) -> bool:
        """Play with pygame.mixer, blocking until done (no external window)."""
        mixer = self._mixer
        if mixer is None:
            try:
                import pygame.mixer as mixer  # type: ignore
            except Exception as exc:
                self.last_error = f"pygame is not installed (pip install pygame): {exc}"
                log.warning(self.last_error)
                return False
            self._mixer = mixer

        budget = min(180.0, 15.0 + path.stat().st_size / 4000.0) + PLAYBACK_PAD
        try:
            if not mixer.get_init():
                mixer.init()
            music = mixer.music
            music.load(str(path))
            music.play()
            deadline = time.monotonic() + budget
            while music.get_busy() and time.monotonic() < deadline:
                time.sleep(0.05)
            music.stop()
            if hasattr(music, "unload"):
                music.unload()
            self.last_tts_engine = "edge-tts+pygame"
            log.info("Spoke via pygame (%s)", path.name)
            return True
        except Exception as exc:
            self.last_error = f"pygame playback failed: {exc}"
            log.warning(self.last_error)
            try:
                mixer.music.stop()
            except Exception:
                pass
            return False

    def _speak_sapi(self, text: str) -> bool:
        """Windows fallback that never writes a media file for playback."""
        if self._sapi is not None:
            try:
                ok = bool(self._sapi(text))
            except Exception as exc:
                self.last_error = f"SAPI failed: {exc}"
                return False
            if ok:
                self.last_tts_engine = "sapi"
            return ok

        if os.name != "nt":
            return False

        script_path: Optional[Path] = None
        try:
            self.audio_dir.mkdir(parents=True, exist_ok=True)
            script_path = self.audio_dir / f"say_{int(time.time() * 1000)}.txt"
            script_path.write_text(text, encoding="utf-8")
            command = (
                "Add-Type -AssemblyName System.Speech; "
                f"$t = Get-Content -Raw -LiteralPath '{script_path}'; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.Speak($t)"
            )
            code, output = _run(
                ["powershell", "-NoProfile", "-Command", command],
                timeout=min(180.0, 10.0 + len(text) / 6.0),
            )
            if code == 0:
                self.last_tts_engine = "sapi"
                return True
            self.last_error = f"SAPI failed: {output[:200]}"
            return False
        except Exception as exc:
            self.last_error = f"SAPI failed: {exc}"
            return False
        finally:
            if script_path is not None:
                script_path.unlink(missing_ok=True)

    def stop_speaking(self) -> None:
        """Stop pygame playback immediately."""
        try:
            if self._mixer is not None:
                self._mixer.music.stop()
        except Exception as exc:  # pragma: no cover
            log.debug("stop_speaking: %s", exc)

    # ------------------------------------------------------------------ #
    # STT
    # ------------------------------------------------------------------ #

    def record(self, seconds: float = 6.0, path: Optional[Path] = None) -> Optional[Path]:
        """Record microphone audio to a WAV file (sounddevice + soundfile)."""
        duration = max(1.0, min(float(seconds), MAX_RECORD_SECONDS))
        target = Path(path) if path else (
            self.record_dir / f"mic_{int(time.time() * 1000)}.wav"
        )

        if self._recorder is not None:
            try:
                recorded = self._recorder(duration, target)
                return Path(recorded) if recorded else None
            except Exception as exc:
                self.last_error = f"recording failed: {exc}"
                return None

        if not (_module_available("sounddevice") and _module_available("soundfile")):
            self.last_error = (
                "মাইক্রোফোন সাপোর্ট নেই — pip install sounddevice soundfile"
            )
            log.warning(self.last_error)
            return None

        try:
            import sounddevice as sd  # type: ignore
            import soundfile as sf  # type: ignore

            self.record_dir.mkdir(parents=True, exist_ok=True)
            sample_rate = 16000
            audio = sd.rec(
                int(duration * sample_rate), samplerate=sample_rate, channels=1
            )
            sd.wait()
            sf.write(str(target), audio, sample_rate)
            if not target.is_file() or target.stat().st_size < 512:
                self.last_error = "recording produced no audio (mic permission?)"
                return None
            return target
        except Exception as exc:
            self.last_error = f"recording failed: {exc}"
            log.warning(self.last_error)
            return None

    def transcribe(self, audio_path: Path, language: Optional[str] = None) -> str:
        """Send an audio file to Groq Whisper and return the text."""
        path = Path(audio_path)
        if not path.is_file():
            raise VoiceError(f"audio file not found: {path}")
        if not getattr(self.settings, "groq_api_key", ""):
            raise VoiceError("GROQ_API_KEY missing — cannot transcribe (see .env.example)")
        if requests is None and self._session is None:
            raise VoiceError("requests is not installed (pip install -r requirements.txt)")

        model = os.environ.get("WHISPER_MODEL", WHISPER_MODEL)
        lang = (language or self.language or WHISPER_LANGUAGE).lower()
        data = {"model": model, "response_format": "json", "temperature": "0"}
        if lang and lang != "auto":
            data["language"] = lang
        data["prompt"] = "বাংলা বা Banglish কথ্য ভাষা।"

        headers = {"Authorization": f"Bearer {self.settings.groq_api_key}"}
        mime = MIME_BY_SUFFIX.get(path.suffix.lower(), "audio/wav")

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
            return self.transcribe(audio, language=language)
        except VoiceError as exc:
            self.last_error = str(exc)
            log.error("Transcription failed: %s", exc)
            return ""
        finally:
            if not keep_audio:
                Path(audio).unlink(missing_ok=True)

    def listen_and_confirm(self, seconds: float = 6.0) -> str:
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
