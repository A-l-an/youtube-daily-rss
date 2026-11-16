from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
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


def metadata_ydl(entries, details=None):
    class FakeYDL:
        calls = []
        options = []

        def __init__(self, options):
            self.options.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            assert not download
            self.calls.append(url)
            if url.endswith('/videos'):
                return {'entries': entries}
            result = (details or {})[url.rsplit('=', 1)[-1]]
            if isinstance(result, Exception):
                raise result
            return result
    return FakeYDL


class FetchYouTubeTests(unittest.TestCase):
    def test_fetch_latest_video_parses_valid_feed(self) -> None:
        channel = {"name": "Example", "channel_id": "UCexample", "handle": "@example"}

        with patch.object(fetch_youtube.requests, "get", return_value=FakeResponse(VALID_FEED)) as get_mock, \
                patch('yt_dlp.YoutubeDL', metadata_ydl([{'id': 'abc123', 'availability': 'public'}])):
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

        with patch.object(fetch_youtube.requests, "get", side_effect=responses) as get_mock, \
                patch('yt_dlp.YoutubeDL', metadata_ydl([{'id': 'abc123', 'availability': 'public'}])):
            video = fetch_youtube.fetch_latest_video(channel)

        self.assertEqual(video.video_id, "abc123")
        self.assertEqual(get_mock.call_count, 2)

    def test_fetch_latest_video_falls_back_to_ytdlp_when_feed_down(self) -> None:
        # YouTube's Atom endpoint has returned site-wide 404s; discovery must survive it.
        channel = {"name": "Example", "channel_id": "UCexample", "handle": "@ex"}
        notfound = FakeResponse(b"<html>404</html>", status_code=404, content_type="text/html")

        class FakeYDL:
            def __init__(self, options):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                return {"entries": [{"id": "xy789", "availability": "public", "title": "Newest", "url": "https://www.youtube.com/watch?v=xy789"}]}

        with patch.object(fetch_youtube.requests, "get", return_value=notfound), \
                patch.dict(sys.modules, {"yt_dlp": SimpleNamespace(YoutubeDL=FakeYDL)}):
            video = fetch_youtube.fetch_latest_video(channel)

        self.assertEqual(video.video_id, "xy789")
        self.assertEqual(video.title, "Newest")
        self.assertEqual(video.channel_name, "Example")

    def test_fetch_latest_video_raises_when_feed_and_ytdlp_both_fail(self) -> None:
        channel = {"name": "Example", "channel_id": "UCexample"}
        blocked = FakeResponse(
            b"<html><body>Service unavailable from upstream</body></html>",
            status_code=503,
            content_type="text/html",
        )

        class FakeYDL:
            def __init__(self, options):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                raise RuntimeError("ytdlp boom")

        with patch.object(fetch_youtube.requests, "get", return_value=blocked) as get_mock, \
                patch.dict(sys.modules, {"yt_dlp": SimpleNamespace(YoutubeDL=FakeYDL)}):
            with self.assertRaisesRegex(RuntimeError, "HTTP 503") as raised:
                fetch_youtube.fetch_latest_video(channel)

        self.assertEqual(get_mock.call_count, fetch_youtube.MAX_FETCH_ATTEMPTS)
        message = str(raised.exception)
        self.assertIn("content-type=text/html", message)
        self.assertIn("body_preview=", message)
        self.assertIn("yt-dlp fallback error", message)

    def test_fallback_skips_members_first_and_selects_public_second_without_detail_probes(self):
        ydl = metadata_ydl([{'id': 'member', 'availability': 'subscriber_only'},
                            {'id': 'public', 'availability': 'public', 'title': 'Public upload'}])
        with patch.object(fetch_youtube, '_fetch_and_parse_feed', side_effect=RuntimeError('Atom outage')), \
                patch('yt_dlp.YoutubeDL', ydl):
            result = fetch_youtube.fetch_latest_video({'channel_id': 'UCexample'})
        self.assertEqual(result.video_id, 'public')
        self.assertIsNone(result.published)
        self.assertEqual(ydl.calls, ['https://www.youtube.com/channel/UCexample/videos'])
        self.assertLessEqual(ydl.options[0]['playlistend'], 10)

    def test_atom_skips_member_and_preserves_public_atom_timestamp(self):
        second_entry = b'<entry>' + VALID_FEED.split(b'<entry>')[1].split(b'</entry>')[0] + b'</entry>'
        feed = VALID_FEED.replace(b'</feed>', second_entry.replace(b'abc123', b'public') + b'</feed>')
        ydl = metadata_ydl([{'id': 'abc123', 'availability': 'subscriber_only'},
                            {'id': 'public', 'availability': 'public'}])
        with patch.object(fetch_youtube.requests, 'get', return_value=FakeResponse(feed)), \
                patch('yt_dlp.YoutubeDL', ydl):
            result = fetch_youtube.fetch_latest_video({'channel_id': 'UCexample'})
        self.assertEqual(result.video_id, 'public')
        self.assertEqual(result.published, '2026-06-04T00:00:00+00:00')

    def test_unknown_flat_candidate_is_confirmed_without_probing_remaining_uploads(self):
        ydl = metadata_ydl([{'id': 'abc123', 'availability': None, 'title': 'Members-only discussion'},
                            {'id': 'older', 'availability': 'public'}],
                           {'abc123': {'id': 'abc123', 'availability': 'public'}})
        with patch.object(fetch_youtube.requests, 'get', return_value=FakeResponse(VALID_FEED)), \
                patch('yt_dlp.YoutubeDL', ydl):
            result = fetch_youtube.fetch_latest_video({'channel_id': 'UCexample'})
        self.assertEqual(result.video_id, 'abc123')
        self.assertEqual(len(ydl.calls), 2)

    def test_unknown_or_transient_failure_never_selects_older_public_upload(self):
        for result in ({'id': 'new', 'availability': None}, RuntimeError('timed out'),
                       RuntimeError('HTTP Error 403: Forbidden'),
                       RuntimeError('Tunnel connection failed: 407 Proxy Authentication Required'),
                       RuntimeError("Sign in to confirm you’re not a bot")):
            with self.subTest(result=result):
                ydl = metadata_ydl([{'id': 'new', 'availability': None},
                                    {'id': 'older', 'availability': 'public'}], {'new': result})
                with patch.object(fetch_youtube, '_fetch_and_parse_feed', side_effect=RuntimeError('Atom outage')), \
                        patch('yt_dlp.YoutubeDL', ydl), self.assertRaises(RuntimeError):
                    fetch_youtube.fetch_latest_video({'channel_id': 'UCexample'})

    def test_confirmed_member_detail_failure_can_skip_to_public(self):
        ydl = metadata_ydl([{'id': 'member', 'availability': None},
                            {'id': 'public', 'availability': 'public'}],
                           {'member': RuntimeError('Join this channel to get access to members-only content like this video')})
        with patch.object(fetch_youtube, '_fetch_and_parse_feed', side_effect=RuntimeError('Atom outage')), \
                patch('yt_dlp.YoutubeDL', ydl):
            result = fetch_youtube.fetch_latest_video({'channel_id': 'UCexample'})
        self.assertEqual(result.video_id, 'public')

    def test_all_restricted_candidates_fail_explicitly_with_bounded_selection(self):
        entries = [{'id': str(i), 'availability': status} for i, status in enumerate(
            ['subscriber_only', 'private', 'needs_auth', 'premium_only'] * 3)]
        entries.append({'id': 'beyond-limit', 'availability': 'public'})
        ydl = metadata_ydl(entries)
        with patch.object(fetch_youtube, '_fetch_and_parse_feed', side_effect=RuntimeError('Atom outage')), \
                patch('yt_dlp.YoutubeDL', ydl), self.assertRaisesRegex(RuntimeError, 'access-restricted'):
            fetch_youtube.fetch_latest_video({'channel_id': 'UCexample'})


if __name__ == "__main__":
    unittest.main()
