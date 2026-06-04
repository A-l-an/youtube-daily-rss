from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
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


if __name__ == "__main__":
    unittest.main()
