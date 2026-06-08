from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from fetch_youtube import Video
import preflight_youtube_feeds


def fake_video(channel_id: str) -> Video:
    return Video(
        channel_name="Example",
        channel_id=channel_id,
        handle="@example",
        video_id="abc123",
        title="Latest Video",
        url="https://www.youtube.com/watch?v=abc123",
        published="2026-06-04T00:00:00+00:00",
    )


class PreflightYouTubeFeedsTests(unittest.TestCase):
    def test_passes_when_at_least_one_configured_feed_is_reachable(self) -> None:
        channels = [
            {"name": "Broken", "channel_id": "UCbroken"},
            {"name": "Working", "channel_id": "UCworking"},
        ]

        def fetch(channel):
            if channel["channel_id"] == "UCbroken":
                raise RuntimeError("HTTP 404")
            return fake_video(channel["channel_id"])

        with patch.object(preflight_youtube_feeds, "fetch_latest_video", side_effect=fetch):
            successes, failures = preflight_youtube_feeds.check_channels(channels)

        self.assertEqual([video.channel_id for video in successes], ["UCworking"])
        self.assertEqual(failures[0][0], "Broken (UCbroken)")
        self.assertIn("HTTP 404", failures[0][1])

    def test_run_fails_when_all_configured_feeds_fail(self) -> None:
        config = {"channels": [{"name": "Broken", "channel_id": "UCbroken"}]}

        with (
            patch.object(preflight_youtube_feeds, "load_config", return_value=config),
            patch.object(preflight_youtube_feeds, "fetch_latest_video", side_effect=RuntimeError("HTTP 404")),
        ):
            self.assertEqual(preflight_youtube_feeds.run(Path("config.yml"), min_success=1), 1)

    def test_run_passes_when_one_configured_feed_is_reachable(self) -> None:
        config = {
            "channels": [
                {"name": "Broken", "channel_id": "UCbroken"},
                {"name": "Working", "channel_id": "UCworking"},
            ]
        }

        def fetch(channel):
            if channel["channel_id"] == "UCbroken":
                raise RuntimeError("HTTP 404")
            return fake_video(channel["channel_id"])

        with (
            patch.object(preflight_youtube_feeds, "load_config", return_value=config),
            patch.object(preflight_youtube_feeds, "fetch_latest_video", side_effect=fetch),
        ):
            self.assertEqual(preflight_youtube_feeds.run(Path("config.yml"), min_success=1), 0)

    def test_run_fails_when_no_channels_are_configured(self) -> None:
        with patch.object(preflight_youtube_feeds, "load_config", return_value={"channels": []}):
            self.assertEqual(preflight_youtube_feeds.run(Path("config.yml"), min_success=1), 1)


if __name__ == "__main__":
    unittest.main()
