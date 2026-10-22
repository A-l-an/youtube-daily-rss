from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import asr


class ASRAudioDownloadTests(unittest.TestCase):
    def test_audio_download_retries_after_transient_ytdlp_error(self) -> None:
        class FakeYoutubeDL:
            attempts = 0
            options_seen = []

            def __init__(self, options):
                self.options = options
                type(self).options_seen.append(options)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def download(self, urls):
                type(self).attempts += 1
                if type(self).attempts == 1:
                    raise RuntimeError("ssl eof")
                Path(self.options["outtmpl"].replace("%(ext)s", "mp3")).write_text("audio", encoding="utf-8")

        fake_module = SimpleNamespace(YoutubeDL=FakeYoutubeDL)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"yt_dlp": fake_module}):
            audio_path = asr._download_audio("https://example.test/watch?v=abc", Path(tmp), attempts=2, sleep_seconds=0)

        self.assertEqual(audio_path.name, "audio.mp3")
        self.assertEqual(FakeYoutubeDL.attempts, 2)
        self.assertEqual(FakeYoutubeDL.options_seen[0]["retries"], 10)
        self.assertEqual(
            FakeYoutubeDL.options_seen[0]["extractor_args"]["youtube"]["player_client"],
            ["android", "mweb", "web"],
        )

    def test_audio_download_reports_attempt_count_after_failures(self) -> None:
        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def download(self, urls):
                raise RuntimeError("still failing")

        fake_module = SimpleNamespace(YoutubeDL=FakeYoutubeDL)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"yt_dlp": fake_module}):
            with self.assertRaisesRegex(RuntimeError, "after 2 attempt"):
                asr._download_audio("https://example.test/watch?v=abc", Path(tmp), attempts=2, sleep_seconds=0)


class ASRCompatibilityTests(unittest.TestCase):
    def test_legacy_providers_still_transcribe_all_chunks_through_sdk(self) -> None:
        class Transcriptions:
            def create(self, *, model, file, response_format, language):
                if (model, response_format, language) != ("whisper-1", "verbose_json", "zh"):
                    raise AssertionError("Legacy SDK request changed")
                return {"text": file.read().decode()}

        client = SimpleNamespace(audio=SimpleNamespace(transcriptions=Transcriptions()))
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / "first.mp3", Path(tmp) / "second.mp3"]
            paths[0].write_bytes(b"first chunk")
            paths[1].write_bytes(b"second chunk")
            for provider in ("hkust_gz", "openai"):
                with (
                    self.subTest(provider=provider),
                    patch.dict(os.environ, {}, clear=True),
                    patch.object(asr, "resolve_ai_credentials", return_value=SimpleNamespace(provider=provider)),
                    patch.object(asr, "make_openai_compatible_client", return_value=client),
                    patch.object(asr, "_transcribe_xiemac_file", side_effect=AssertionError("Unexpected xiemac call")),
                    patch.object(asr.shutil, "which", return_value="ffmpeg"),
                    patch.object(asr, "_download_audio", return_value=paths[0]),
                    patch.object(asr, "_chunk_audio", return_value=paths),
                    patch.object(asr.time, "sleep"),
                ):
                    result = asr.transcribe_video("https://example.test/video", {
                        "enabled": True, "provider": provider, "model": "whisper-1", "language_candidates": ["zh"],
                    })
                    self.assertEqual(result.status, "success", result.error_message)
                    self.assertEqual(result.text, "first chunk\n\nsecond chunk")

    def test_existing_chunker_preserves_final_short_chunk(self) -> None:
        from pydub import AudioSegment

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "audio.wav"
            AudioSegment.silent(duration=121000, frame_rate=8000).export(source, format="wav").close()
            paths = asr._chunk_audio(source, root, 60)
            self.assertEqual([len(AudioSegment.from_file(path)) for path in paths], [60000, 60000, 1000])


if __name__ == "__main__":
    unittest.main()
