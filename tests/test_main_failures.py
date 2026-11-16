from __future__ import annotations

import argparse
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import main as main_module
from asr import ASRResult
from fetch_youtube import Video
from transcript import TranscriptResult


class MainFailurePolicyTests(unittest.TestCase):
    def test_confirmed_caption_restriction_skips_audio_attempt_and_marks_blocked(self):
        video = Video('Example', 'UCexample', '@example', 'abc123', 'Example',
                      'https://www.youtube.com/watch?v=abc123', None)
        with patch.object(main_module, 'get_transcript', return_value=TranscriptResult(
                status='failed', error_message='This video is available to members only')), \
                patch.object(main_module, 'transcribe_video', side_effect=AssertionError('audio must not run')):
            record = main_module.collect_video_content(video, {}, dry_run=False)
        self.assertEqual(record['access_status'], 'blocked')
        self.assertEqual(record['access_reason'], 'members-only')
        self.assertEqual(record['text'], '')

    def test_transient_and_bot_errors_are_never_access_blocked(self):
        video = Video('Example', 'UCexample', '@example', 'abc123', 'Example',
                      'https://www.youtube.com/watch?v=abc123', None)
        for error in ('remote_busy', 'timeout', 'HTTP Error 403: Forbidden',
                      'Tunnel connection failed: 407 Proxy Authentication Required',
                      "Sign in to confirm you’re not a bot", 'remote_unavailable'):
            with self.subTest(error=error), \
                    patch.object(main_module, 'get_transcript', return_value=TranscriptResult(status='failed', error_message=error)), \
                    patch.object(main_module, 'transcribe_video', return_value=ASRResult(status='failed', error_message=error)):
                record = main_module.collect_video_content(video, {}, dry_run=False)
                self.assertNotEqual(record.get('access_status'), 'blocked')
                self.assertEqual(record['asr_status'], 'failed')

    def test_all_channel_fetch_failures_keep_existing_outputs_and_succeed(self) -> None:
        args = argparse.Namespace(dry_run=False, force=False)

        with (
            patch.object(main_module, "fetch_latest_video", side_effect=RuntimeError("feed unavailable")) as fetch_mock,
            patch.object(main_module, "write_rss") as write_rss_mock,
            patch.object(main_module, "save_json") as save_json_mock,
        ):
            with self.assertLogs(level="ERROR") as logs:
                exit_code = main_module.run(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(fetch_mock.call_count, 2)
        self.assertIn("keeping existing RSS", "\n".join(logs.output))
        write_rss_mock.assert_not_called()
        save_json_mock.assert_not_called()

    def test_subtitle_fallback_success_skips_asr(self) -> None:
        video = Video(
            channel_name="Example",
            channel_id="UCexample",
            handle="@example",
            video_id="abc123",
            title="Example Video",
            url="https://www.youtube.com/watch?v=abc123",
            published="2026-06-04T00:00:00+00:00",
        )
        config = {"transcript": {}, "asr": {}}
        transcript_result = TranscriptResult(
            status="success",
            language="zh",
            source="yt_dlp_manual_subtitle",
            text="subtitle text",
        )

        with (
            patch.object(main_module, "get_transcript", return_value=transcript_result),
            patch.object(main_module, "transcribe_video") as asr_mock,
        ):
            record = main_module.collect_video_content(video, config, dry_run=False)

        self.assertEqual(record["source_status"], "captions")
        self.assertEqual(record["content_source"], "YouTube captions")
        self.assertEqual(record["transcript_source"], "yt_dlp_manual_subtitle")
        self.assertEqual(record["text"], "subtitle text")
        asr_mock.assert_not_called()

    def test_transcript_and_asr_failures_return_link_only_record(self) -> None:
        video = Video(
            channel_name="Example",
            channel_id="UCexample",
            handle="@example",
            video_id="abc123",
            title="Example Video",
            url="https://www.youtube.com/watch?v=abc123",
            published="2026-06-04T00:00:00+00:00",
        )
        config = {"transcript": {}, "asr": {}}

        with (
            patch.object(
                main_module,
                "get_transcript",
                return_value=TranscriptResult(status="failed", error_message="blocked"),
            ),
            patch.object(
                main_module,
                "transcribe_video",
                return_value=ASRResult(status="failed", model="whisper-1", error_message="download blocked"),
            ),
        ):
            record = main_module.collect_video_content(video, config, dry_run=False)

        self.assertEqual(record["source_status"], "link-only")
        self.assertEqual(record["content_source"], "metadata only")
        self.assertEqual(record["text"], "")
        self.assertEqual(record["asr_status"], "failed")


if __name__ == "__main__":
    unittest.main()
