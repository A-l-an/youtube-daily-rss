from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import transcript


class FakeSnippet:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeFetched:
    def __init__(self, texts: list[str]) -> None:
        self.snippets = [FakeSnippet(text) for text in texts]


class FakeTranscript:
    def __init__(self, language_code: str, is_generated: bool, texts: list[str]) -> None:
        self.language_code = language_code
        self.is_generated = is_generated
        self._texts = texts

    def fetch(self) -> FakeFetched:
        return FakeFetched(self._texts)


class TranscriptFallbackTests(unittest.TestCase):
    def test_primary_transcript_success_does_not_call_ytdlp(self) -> None:
        config = {
            "preferred_languages": ["zh-Hans", "zh"],
            "allow_auto_captions": True,
            "yt_dlp_fallback": {"enabled": True},
        }

        with (
            patch.object(
                transcript,
                "_list_transcripts",
                return_value=[FakeTranscript("zh-Hans", False, ["hello", "world"])],
            ),
            patch.object(transcript, "_get_yt_dlp_transcript") as fallback_mock,
        ):
            result = transcript.get_transcript("abc123", config)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.source, "manual_caption")
        self.assertEqual(result.text, "hello\nworld")
        fallback_mock.assert_not_called()

    def test_ytdlp_fallback_after_transcript_api_block(self) -> None:
        config = {
            "preferred_languages": ["zh-Hans"],
            "allow_auto_captions": True,
            "yt_dlp_fallback": {"enabled": True},
        }
        fallback = transcript.TranscriptResult(
            status="success",
            language="zh-Hans",
            source="yt_dlp_manual_subtitle",
            text="fallback text",
        )

        with (
            patch.object(
                transcript,
                "_list_transcripts",
                side_effect=RuntimeError("YouTube is blocking requests from your IP"),
            ),
            patch.object(transcript, "_get_yt_dlp_transcript", return_value=fallback),
        ):
            result = transcript.get_transcript("abc123", config)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.source, "yt_dlp_manual_subtitle")
        self.assertEqual(result.text, "fallback text")

    def test_ytdlp_manual_subtitles_precede_auto_captions_and_follow_language_order(self) -> None:
        config = {
            "preferred_languages": ["zh-Hans", "zh", "en"],
            "allow_auto_captions": True,
            "yt_dlp_fallback": {
                "enabled": True,
                "subtitle_format": "vtt",
                "sleep_interval_seconds": 0,
            },
        }
        calls: list[tuple[str, bool]] = []

        def fake_download(
            video_id: str,
            language: str,
            generated: bool,
            subtitle_format: str,
            output_dir: Path,
        ) -> Path:
            calls.append((language, generated))
            if (language, generated) != ("zh", False):
                raise RuntimeError("not available")
            path = output_dir / "subtitle-zh.vtt"
            path.write_text(
                "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nmanual zh text\n",
                encoding="utf-8",
            )
            return path

        with (
            patch.object(transcript, "_list_transcripts", return_value=[]),
            patch.object(transcript, "_download_yt_dlp_subtitle", side_effect=fake_download),
        ):
            result = transcript.get_transcript("abc123", config)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.language, "zh")
        self.assertEqual(result.source, "yt_dlp_manual_subtitle")
        self.assertEqual(result.text, "manual zh text")
        self.assertEqual(calls, [("zh-Hans", False), ("zh", False)])

    def test_ytdlp_auto_caption_used_after_manual_subtitles_fail(self) -> None:
        config = {
            "preferred_languages": ["zh-Hans"],
            "allow_auto_captions": True,
            "yt_dlp_fallback": {
                "enabled": True,
                "subtitle_format": "vtt",
                "sleep_interval_seconds": 0,
            },
        }
        calls: list[tuple[str, bool]] = []

        def fake_download(
            video_id: str,
            language: str,
            generated: bool,
            subtitle_format: str,
            output_dir: Path,
        ) -> Path:
            calls.append((language, generated))
            if not generated:
                raise RuntimeError("manual not available")
            path = output_dir / "subtitle-auto.vtt"
            path.write_text(
                "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nauto text\n",
                encoding="utf-8",
            )
            return path

        with (
            patch.object(transcript, "_list_transcripts", return_value=[]),
            patch.object(transcript, "_download_yt_dlp_subtitle", side_effect=fake_download),
        ):
            result = transcript.get_transcript("abc123", config)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.language, "zh-Hans")
        self.assertEqual(result.source, "yt_dlp_auto_caption")
        self.assertEqual(result.text, "auto text")
        self.assertEqual(calls, [("zh-Hans", False), ("zh-Hans", True)])


if __name__ == "__main__":
    unittest.main()
