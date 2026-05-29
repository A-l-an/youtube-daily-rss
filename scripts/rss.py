from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import xml.etree.ElementTree as ET


def _parse_datetime(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except Exception:
            dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _rss_date(value: Optional[str]) -> str:
    return format_datetime(_parse_datetime(value))


def _sort_key(item: Dict[str, Any]) -> datetime:
    return _parse_datetime(item.get("published") or item.get("processed_at"))


def build_rss(config: Dict[str, Any], items: List[Dict[str, Any]]) -> ET.ElementTree:
    rss_config = config.get("rss", {})
    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = rss_config.get("title", "Daily YouTube Finance Digest")
    ET.SubElement(channel, "link").text = rss_config.get("link", "")
    ET.SubElement(channel, "description").text = rss_config.get("description", "")
    ET.SubElement(channel, "language").text = config.get("summary", {}).get("language", "zh-CN")
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))

    for digest in sorted(items, key=_sort_key, reverse=True):
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = f"[{digest.get('channel_name')}] {digest.get('title')}"
        ET.SubElement(item, "link").text = digest.get("url")
        guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
        guid.text = digest.get("video_id")
        ET.SubElement(item, "pubDate").text = _rss_date(digest.get("published") or digest.get("processed_at"))
        ET.SubElement(item, "description").text = digest.get("summary_html", "")
        for category in ["YouTube Digest", "Finance", "Stock Market", digest.get("source_status", "link-only")]:
            ET.SubElement(item, "category").text = category

    return ET.ElementTree(root)


def write_rss(config: Dict[str, Any], items: List[Dict[str, Any]], root_dir: Path) -> Path:
    output_path = root_dir / config.get("rss", {}).get("output_path", "public/feed.xml")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree = build_rss(config, items)
    ET.indent(tree, space="  ")
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    return output_path
