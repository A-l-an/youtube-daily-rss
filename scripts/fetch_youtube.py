from __future__ import annotations

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


def fetch_latest_video(channel: Dict[str, Any]) -> Video:
    channel_id = channel["channel_id"]
    feed_url = YOUTUBE_FEED_URL.format(channel_id=channel_id)
    parsed = _fetch_and_parse_feed(feed_url, channel_id)

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
