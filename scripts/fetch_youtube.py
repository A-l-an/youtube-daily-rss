from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

import feedparser
import requests


YOUTUBE_FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
REQUEST_TIMEOUT_SECONDS = 20
MAX_FETCH_ATTEMPTS = 3
BODY_PREVIEW_CHARS = 500
FEED_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
}


@dataclass
class Video:
    channel_name: str
    channel_id: str
    handle: str
    video_id: str
    title: str
    url: str
    published: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _safe_body_preview(content: bytes) -> str:
    text = content[: BODY_PREVIEW_CHARS * 2].decode("utf-8", errors="replace")
    return " ".join(text.split())[:BODY_PREVIEW_CHARS]


def _response_diagnostic(response: requests.Response, parse_error: Optional[BaseException] = None) -> str:
    parts = [
        f"HTTP {response.status_code}",
        f"content-type={response.headers.get('content-type', 'unknown')}",
    ]
    if parse_error is not None:
        parts.append(f"parse_error={parse_error}")
    preview = _safe_body_preview(response.content)
    if preview:
        parts.append(f"body_preview={preview!r}")
    return "; ".join(parts)


def _parse_feed(content: bytes, channel_id: str):
    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"Failed to parse YouTube feed for {channel_id}: {parsed.bozo_exception}")
    if not parsed.entries:
        raise RuntimeError(f"No public videos found in YouTube feed for {channel_id}")
    return parsed


def _fetch_and_parse_feed(feed_url: str, channel_id: str):
    last_error = ""
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        try:
            response = requests.get(feed_url, headers=FEED_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
            if response.status_code >= 400:
                last_error = f"attempt {attempt}: {_response_diagnostic(response)}"
                continue
            try:
                return _parse_feed(response.content, channel_id)
            except RuntimeError as exc:
                last_error = f"attempt {attempt}: {_response_diagnostic(response, exc)}"
        except requests.RequestException as exc:
            last_error = f"attempt {attempt}: request_error={exc}"

    raise RuntimeError(f"Failed to fetch latest YouTube feed for {channel_id}: {last_error}")


def _fetch_latest_via_ytdlp(channel: Dict[str, Any], feed_exc: Exception) -> Video:
    """Discovery fallback for when YouTube's Atom feed endpoint is down/404.

    Lists the channel's newest upload via yt-dlp (channel /videos tab), reusing the
    cookies + impersonate + EJS hardening from ytdlp_opts. The /feeds/videos.xml
    endpoint has been observed returning Google 404 pages site-wide while the
    channel page itself stays reachable.
    """
    channel_id = channel["channel_id"]
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    options: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlistend": 1,
    }
    try:
        import ytdlp_opts

        ytdlp_opts.merge_into(options, audio=False)
    except Exception as exc:  # hardening is best-effort
        logging.warning("ytdlp_opts.merge_into (discovery) skipped: %s", exc)

    try:
        import yt_dlp

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as ytdlp_exc:
        raise RuntimeError(
            f"Failed to fetch latest video for {channel_id}: "
            f"Atom feed error [{feed_exc}]; yt-dlp fallback error [{ytdlp_exc}]"
        ) from ytdlp_exc

    entries = [e for e in ((info or {}).get("entries") or []) if e and e.get("id")]
    if not entries:
        raise RuntimeError(
            f"Failed to fetch latest video for {channel_id}: "
            f"Atom feed error [{feed_exc}]; yt-dlp fallback returned no videos"
        )

    entry = entries[0]
    video_id = entry["id"]
    return Video(
        channel_name=channel.get("name", channel_id),
        channel_id=channel_id,
        handle=channel.get("handle", ""),
        video_id=video_id,
        title=entry.get("title") or f"YouTube video {video_id}",
        url=entry.get("url") or entry.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}",
        published=None,
    )


def fetch_latest_video(channel: Dict[str, Any]) -> Video:
    channel_id = channel["channel_id"]
    feed_url = YOUTUBE_FEED_URL.format(channel_id=channel_id)
    try:
        parsed = _fetch_and_parse_feed(feed_url, channel_id)
    except Exception as feed_exc:
        logging.warning(
            "[%s] Atom feed unavailable (%s); falling back to yt-dlp channel discovery",
            channel.get("name", channel_id),
            feed_exc,
        )
        return _fetch_latest_via_ytdlp(channel, feed_exc)

    entry = parsed.entries[0]
    video_id = getattr(entry, "yt_videoid", None)
    if not video_id:
        entry_id = getattr(entry, "id", "")
        video_id = entry_id.rsplit(":", 1)[-1] if entry_id else None
    if not video_id:
        raise RuntimeError(f"Could not determine video ID for latest entry in {channel_id}")

    return Video(
        channel_name=channel.get("name", getattr(parsed.feed, "title", channel_id)),
        channel_id=channel_id,
        handle=channel.get("handle", ""),
        video_id=video_id,
        title=getattr(entry, "title", f"YouTube video {video_id}"),
        url=getattr(entry, "link", f"https://www.youtube.com/watch?v={video_id}"),
        published=getattr(entry, "published", None),
    )
