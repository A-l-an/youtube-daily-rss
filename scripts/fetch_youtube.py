from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

import feedparser


YOUTUBE_FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


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


def fetch_latest_video(channel: Dict[str, Any]) -> Video:
    channel_id = channel["channel_id"]
    feed_url = YOUTUBE_FEED_URL.format(channel_id=channel_id)
    parsed = feedparser.parse(feed_url)

    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"Failed to parse YouTube feed for {channel_id}: {parsed.bozo_exception}")

    if not parsed.entries:
        raise RuntimeError(f"No public videos found in YouTube feed for {channel_id}")

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
