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

    def test_run_allows_transient_network_outage_when_enabled(self) -> None:
        config = {
            "channels": [
                {"name": "Proxy Flake 1", "channel_id": "UCflake1"},
                {"name": "Proxy Flake 2", "channel_id": "UCflake2"},
            ]
        }
        error = (
            "Failed to fetch latest YouTube feed: attempt 3: "
            "request_error=HTTPSConnectionPool(host='www.youtube.com'): SSLEOFError"
        )

        with (
            patch.object(preflight_youtube_feeds, "load_config", return_value=config),
            patch.object(preflight_youtube_feeds, "fetch_latest_video", side_effect=RuntimeError(error)),
        ):
            self.assertEqual(
                preflight_youtube_feeds.run(
                    Path("config.yml"),
                    min_success=1,
                    allow_transient_outage=True,
                ),
                0,
            )

    def test_run_allows_server_error_outage_when_enabled(self) -> None:
        config = {
            "channels": [
                {"name": "Server Error 1", "channel_id": "UCerr1"},
                {"name": "Server Error 2", "channel_id": "UCerr2"},
            ]
        }
        error = (
            "Failed to fetch latest YouTube feed for UCerr1: attempt 3: "
            "HTTP 500; content-type=text/html; charset=UTF-8"
        )

        with (
            patch.object(preflight_youtube_feeds, "load_config", return_value=config),
            patch.object(preflight_youtube_feeds, "fetch_latest_video", side_effect=RuntimeError(error)),
        ):
            self.assertEqual(
                preflight_youtube_feeds.run(
                    Path("config.yml"),
                    min_success=1,
                    allow_transient_outage=True,
                ),
                0,
            )

    def test_run_allows_404_corroborated_by_transient_failure_when_enabled(self) -> None:
        config = {
            "channels": [
                {"name": "Server Error", "channel_id": "UCerr"},
                {"name": "Phantom 404", "channel_id": "UCgone"},
            ]
        }
        errors = {
            "UCerr": (
                "Failed to fetch latest YouTube feed for UCerr: attempt 3: "
                "HTTP 500; content-type=text/html; charset=UTF-8"
            ),
            "UCgone": (
                "Failed to fetch latest YouTube feed for UCgone: attempt 3: "
                "HTTP 404; content-type=text/html; charset=UTF-8"
            ),
        }

        def fetch(channel):
            raise RuntimeError(errors[channel["channel_id"]])

        with (
            patch.object(preflight_youtube_feeds, "load_config", return_value=config),
            patch.object(preflight_youtube_feeds, "fetch_latest_video", side_effect=fetch),
        ):
            self.assertEqual(
                preflight_youtube_feeds.run(
                    Path("config.yml"),
                    min_success=1,
                    allow_transient_outage=True,
                ),
                0,
            )

    def test_run_still_fails_when_every_failure_is_404_when_enabled(self) -> None:
        config = {
            "channels": [
                {"name": "Gone 1", "channel_id": "UCgone1"},
                {"name": "Gone 2", "channel_id": "UCgone2"},
            ]
        }
        error = "Failed to fetch latest YouTube feed: attempt 3: HTTP 404; content-type=text/html"

        with (
            patch.object(preflight_youtube_feeds, "load_config", return_value=config),
            patch.object(preflight_youtube_feeds, "fetch_latest_video", side_effect=RuntimeError(error)),
        ):
            self.assertEqual(
                preflight_youtube_feeds.run(
                    Path("config.yml"),
                    min_success=1,
                    allow_transient_outage=True,
                ),
                1,
            )

    def test_run_still_fails_for_non_transient_outage_when_enabled(self) -> None:
        config = {"channels": [{"name": "Broken", "channel_id": "UCbroken"}]}

        with (
            patch.object(preflight_youtube_feeds, "load_config", return_value=config),
            patch.object(preflight_youtube_feeds, "fetch_latest_video", side_effect=RuntimeError("HTTP 404")),
        ):
            self.assertEqual(
                preflight_youtube_feeds.run(
                    Path("config.yml"),
                    min_success=1,
                    allow_transient_outage=True,
                ),
                1,
            )

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
