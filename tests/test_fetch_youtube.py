from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fetch_youtube


VALID_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
  <title>Example Channel</title>
  <entry>
    <id>yt:video:abc123</id>
    <yt:videoId>abc123</yt:videoId>
    <title>Latest Video</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=abc123"/>
    <published>2026-06-04T00:00:00+00:00</published>
  </entry>
</feed>
"""


class FakeResponse:
    def __init__(
        self,
        content: bytes,
        status_code: int = 200,
        content_type: str = "text/xml; charset=UTF-8",
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = {"content-type": content_type}


class FetchYouTubeTests(unittest.TestCase):
    def test_fetch_latest_video_parses_valid_feed(self) -> None:
        channel = {"name": "Example", "channel_id": "UCexample", "handle": "@example"}

        with patch.object(fetch_youtube.requests, "get", return_value=FakeResponse(VALID_FEED)) as get_mock:
            video = fetch_youtube.fetch_latest_video(channel)

        self.assertEqual(video.video_id, "abc123")
        self.assertEqual(video.channel_name, "Example")
        self.assertEqual(video.title, "Latest Video")
        self.assertEqual(video.url, "https://www.youtube.com/watch?v=abc123")

        _, kwargs = get_mock.call_args
        self.assertIn("User-Agent", kwargs["headers"])
        self.assertEqual(kwargs["timeout"], fetch_youtube.REQUEST_TIMEOUT_SECONDS)

    def test_fetch_latest_video_retries_after_malformed_feed(self) -> None:
        channel = {"name": "Example", "channel_id": "UCexample"}
        responses = [
            FakeResponse(b"<html><body>blocked <"),
            FakeResponse(VALID_FEED),
        ]

        with patch.object(fetch_youtube.requests, "get", side_effect=responses) as get_mock:
            video = fetch_youtube.fetch_latest_video(channel)

        self.assertEqual(video.video_id, "abc123")
        self.assertEqual(get_mock.call_count, 2)

    def test_fetch_latest_video_raises_with_response_diagnostic(self) -> None:
        channel = {"name": "Example", "channel_id": "UCexample"}
        blocked = FakeResponse(
            b"<html><body>Service unavailable from upstream</body></html>",
            status_code=503,
            content_type="text/html",
        )

        with patch.object(fetch_youtube.requests, "get", return_value=blocked) as get_mock:
            with self.assertRaisesRegex(RuntimeError, "HTTP 503") as raised:
                fetch_youtube.fetch_latest_video(channel)

        self.assertEqual(get_mock.call_count, fetch_youtube.MAX_FETCH_ATTEMPTS)
        message = str(raised.exception)
        self.assertIn("content-type=text/html", message)
        self.assertIn("body_preview=", message)


if __name__ == "__main__":
    unittest.main()
