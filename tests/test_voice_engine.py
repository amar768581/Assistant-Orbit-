"""
tests/test_voice_engine.py — TTS playback, temp-file cleanup and fallbacks with
a fake pygame mixer: nothing is spoken out loud, no network is touched.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import voice_engine as ve
from voice_engine import VoiceEngine


class FakeMusic:
    def __init__(self, busy=False, fail=False):
        self.busy = busy
        self.fail = fail
        self.loaded = []
        self.played = 0
        self.stopped = 0
        self.unloaded = 0

    def load(self, path):
        self.loaded.append(path)
        if self.fail:
            raise RuntimeError("decoder exploded")

    def play(self):
        self.played += 1

    def get_busy(self):
        if self.busy:
            self.busy = False
            return True
        return False

    def stop(self):
        self.stopped += 1

    def unload(self):
        self.unloaded += 1


class FakeMixer:
    def __init__(self, music=None):
        self.music = music or FakeMusic()
        self.initialized = True

    def init(self):
        self.initialized = True

    def get_init(self):
        return self.initialized


class Engine(VoiceEngine):
    """VoiceEngine with edge-tts replaced by a local dummy file."""

    def __init__(self, **kwargs):
        self.synthesized = []
        super().__init__(
            settings=SimpleNamespace(groq_api_key="", language="bn-IN"),
            **kwargs,
        )

    def synthesize(self, text, voice=None, rate="+0%", volume="+0%"):
        self.synthesized.append(text)
        target = self.audio_dir / f"fake_{len(self.synthesized)}.mp3"
        target.write_bytes(b"x" * 2048)
        return target


def make(**kwargs):
    tmp = tempfile.mkdtemp(prefix="orbit_voice_")
    engine = Engine(
        audio_dir=Path(tmp) / "tts",
        record_dir=Path(tmp) / "rec",
        **kwargs,
    )
    return engine, Path(tmp)


class TestSpeak(unittest.TestCase):
    def test_playback_and_temp_file_deletion(self):
        mixer = FakeMixer()
        engine, _ = make(mixer=mixer)
        self.assertTrue(engine.speak("নমস্কার বস"))
        self.assertEqual(engine.last_tts_engine, "edge-tts+pygame")
        self.assertEqual(mixer.music.played, 1)
        self.assertEqual(len(mixer.music.loaded), 1)
        self.assertFalse(Path(mixer.music.loaded[0]).exists())
        self.assertIsNotNone(engine.last_audio_path)
        self.assertFalse(engine.last_audio_path.exists())

    def test_playback_failure_falls_back_to_sapi(self):
        spoken = []
        engine, _ = make(
            mixer=FakeMixer(FakeMusic(fail=True)),
            sapi=lambda text: spoken.append(text) or True,
        )
        self.assertTrue(engine.speak("পরীক্ষা"))
        self.assertEqual(spoken, ["পরীক্ষা"])
        self.assertEqual(engine.last_tts_engine, "sapi")

    def test_both_fail_returns_false(self):
        engine, _ = make(mixer=FakeMixer(FakeMusic(fail=True)), sapi=lambda text: False)
        self.assertFalse(engine.speak("পরীক্ষা"))
        self.assertIn("pygame", engine.last_error)

    def test_empty_text_does_nothing(self):
        engine, _ = make()
        self.assertFalse(engine.speak("   "))
        self.assertEqual(engine.synthesized, [])

    def test_no_fallback_when_disabled(self):
        engine, _ = make(mixer=FakeMixer(FakeMusic(fail=True)), sapi=lambda text: True)
        self.assertFalse(engine.speak("কিছু", fallback_to_sapi=False))

    def test_stop_speaking(self):
        mixer = FakeMixer()
        engine, _ = make(mixer=mixer)
        engine.stop_speaking()
        self.assertEqual(mixer.music.stopped, 1)


class TestCapabilities(unittest.TestCase):
    def test_diagnostics_shape(self):
        engine, _ = make(mixer=FakeMixer())
        report = engine.diagnostics()
        for key in ("edge-tts", "pygame", "sapi", "sounddevice", "groq_key"):
            self.assertIn(key, report)

    def test_can_record_is_false_without_backend(self):
        engine, _ = make(mixer=FakeMixer())
        with mock.patch.object(ve, "_module_available", return_value=False):
            self.assertFalse(engine.can_record)
            self.assertIsNone(engine.record(seconds=1))
        self.assertIn("মাইক্রোফোন", engine.last_error)

    def test_injected_recorder_is_used(self):
        target = Path(tempfile.mkdtemp()) / "mic.wav"
        target.write_bytes(b"x" * 1024)

        def recorder(seconds, path):
            return target

        engine, _ = make(mixer=FakeMixer(), recorder=recorder)
        self.assertEqual(engine.record(seconds=2), target)


class TestListen(unittest.TestCase):
    def test_listen_deletes_audio_when_done(self):
        target = Path(tempfile.mkdtemp()) / "mic.wav"
        target.write_bytes(b"x" * 1024)

        engine, _ = make(mixer=FakeMixer(), recorder=lambda s, p: target)
        engine.transcribe = lambda path, language=None: "টর্চ জালাও"
        self.assertEqual(engine.listen(seconds=1), "টর্চ জালাও")
        self.assertFalse(target.exists())

    def test_listen_keeps_audio_on_request(self):
        target = Path(tempfile.mkdtemp()) / "mic.wav"
        target.write_bytes(b"x" * 1024)
        engine, _ = make(mixer=FakeMixer(), recorder=lambda s, p: target)
        engine.transcribe = lambda path, language=None: ""
        engine.listen(seconds=1, keep_audio=True)
        self.assertTrue(target.exists())

    def test_listen_without_recording_returns_empty(self):
        engine, _ = make(mixer=FakeMixer(), recorder=lambda s, p: None)
        self.assertEqual(engine.listen(seconds=1), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
