from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

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


def _video_from_entry(channel: Dict[str, Any], parsed: Any, entry: Any) -> Video:
    channel_id = channel["channel_id"]
    video_id = getattr(entry, "yt_videoid", None)
    if not video_id:
        entry_id = getattr(entry, "id", "")
        video_id = entry_id.rsplit(":", 1)[-1] if entry_id else None
    if not video_id:
        raise RuntimeError(f"Could not determine video ID for entry in {channel_id}")

    return Video(
        channel_name=channel.get("name", getattr(parsed.feed, "title", channel_id)),
        channel_id=channel_id,
        handle=channel.get("handle", ""),
        video_id=video_id,
        title=getattr(entry, "title", f"YouTube video {video_id}"),
        url=getattr(entry, "link", f"https://www.youtube.com/watch?v={video_id}"),
        published=getattr(entry, "published", None),
    )


def _trusted_published_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing published timestamp")
    try:
        published = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid published timestamp: {value!r}") from exc
    if published.tzinfo is None:
        raise ValueError(f"published timestamp has no timezone: {value!r}")
    return published.astimezone(timezone.utc)


def fetch_video_for_report_date(
    channel: Dict[str, Any],
    report_date: date,
    timezone_name: str,
) -> Video:
    """Return the latest trusted Atom entry before the report-date cutoff.

    Historical selection deliberately has no yt-dlp fallback: flat channel
    discovery does not provide a trustworthy publication timestamp, so using it
    could silently select a video from after the requested date.
    """
    channel_id = channel["channel_id"]
    try:
        local_timezone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise RuntimeError(f"Invalid report timezone {timezone_name!r}") from exc

    cutoff_local = datetime.combine(report_date + timedelta(days=1), time.min, tzinfo=local_timezone)
    cutoff_utc = cutoff_local.astimezone(timezone.utc)
    feed_url = YOUTUBE_FEED_URL.format(channel_id=channel_id)
    parsed = _fetch_and_parse_feed(feed_url, channel_id)

    candidates = []
    for entry in parsed.entries:
        try:
            published = _trusted_published_datetime(getattr(entry, "published", None))
            video = _video_from_entry(channel, parsed, entry)
        except (RuntimeError, ValueError) as exc:
            raise RuntimeError(
                f"Untrusted Atom entry for {channel_id}; refusing historical selection: {exc}"
            ) from exc
        if published < cutoff_utc:
            candidates.append((published, video))

    if not candidates:
        raise RuntimeError(
            f"No Atom entry with a trustworthy published timestamp exists before "
            f"the {report_date.isoformat()} cutoff for {channel_id}"
        )
    return max(candidates, key=lambda candidate: candidate[0])[1]


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

    return _video_from_entry(channel, parsed, parsed.entries[0])
