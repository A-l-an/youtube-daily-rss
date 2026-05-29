from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
import html
from pathlib import Path
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin
import xml.etree.ElementTree as ET


ATOM_NS = "http://www.w3.org/2005/Atom"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

ET.register_namespace("atom", ATOM_NS)
ET.register_namespace("content", CONTENT_NS)


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


def _escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _ensure_trailing_slash(value: str) -> str:
    return value if value.endswith("/") else f"{value}/"


def _site_url(config: Dict[str, Any]) -> str:
    rss_config = config.get("rss", {})
    site_url = rss_config.get("site_url")
    if site_url:
        return _ensure_trailing_slash(site_url)

    link = rss_config.get("link", "")
    if not link:
        return ""
    if link.endswith(".xml"):
        return _ensure_trailing_slash(link.rsplit("/", 1)[0])
    return _ensure_trailing_slash(link)


def _feed_url(config: Dict[str, Any]) -> str:
    rss_config = config.get("rss", {})
    if rss_config.get("feed_url"):
        return rss_config["feed_url"]

    link = rss_config.get("link", "")
    if link.endswith(".xml"):
        return link

    site_url = _site_url(config)
    if site_url:
        return urljoin(site_url, "feed.xml")
    return ""


def _safe_video_id(video_id: Any) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(video_id or "unknown")).strip("_")
    return safe or "unknown"


def item_page_url(config: Dict[str, Any], digest: Dict[str, Any]) -> str:
    relative_path = f"items/{_safe_video_id(digest.get('video_id'))}.html"
    site_url = _site_url(config)
    return urljoin(site_url, relative_path) if site_url else relative_path


def _page_styles() -> str:
    return """
    :root {
      color-scheme: light dark;
      --bg: #f8fafc;
      --fg: #111827;
      --muted: #64748b;
      --line: #dbe3ee;
      --accent: #0f766e;
      --panel: #ffffff;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #0f172a;
        --fg: #e5e7eb;
        --muted: #9ca3af;
        --line: #263244;
        --accent: #5eead4;
        --panel: #111827;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--fg);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.65;
    }
    main {
      width: min(920px, calc(100% - 32px));
      margin: 0 auto;
      padding: 32px 0 48px;
    }
    header, article, footer {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 24px;
    }
    header { margin-bottom: 16px; }
    article { overflow-wrap: anywhere; }
    footer { margin-top: 16px; color: var(--muted); }
    h1, h2, h3 { line-height: 1.25; }
    h1 { margin: 0 0 12px; font-size: clamp(1.65rem, 4vw, 2.35rem); }
    h2 { margin-top: 0; }
    h3 { margin-top: 1.4em; }
    p { margin: 0.65em 0; }
    ul { padding-left: 1.25em; }
    a { color: var(--accent); }
    .eyebrow {
      margin: 0 0 8px;
      color: var(--muted);
      font-size: 0.86rem;
      letter-spacing: 0;
      text-transform: uppercase;
    }
    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 16px;
    }
    .pill {
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      padding: 4px 10px;
      font-size: 0.9rem;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 18px;
    }
    .button {
      border: 1px solid var(--accent);
      border-radius: 6px;
      color: var(--accent);
      display: inline-block;
      padding: 7px 11px;
      text-decoration: none;
    }
    section {
      border-top: 1px solid var(--line);
      margin-top: 18px;
      padding-top: 12px;
    }
    small { color: var(--muted); font-weight: 500; }
    """


def _render_item_page(config: Dict[str, Any], digest: Dict[str, Any]) -> str:
    title = f"[{digest.get('channel_name')}] {digest.get('title')}"
    feed_url = _feed_url(config)
    original_url = digest.get("url") or ""
    source_status = digest.get("source_status") or "link-only"
    summary_status = digest.get("summary_status") or "unknown"
    summary_html = digest.get("summary_html") or "<p>No digest is available for this item.</p>"

    alternate = ""
    if feed_url:
        alternate = (
            f'<link rel="alternate" type="application/rss+xml" '
            f'title="{_escape(config.get("rss", {}).get("title", "RSS Feed"))}" href="{_escape(feed_url)}">'
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_escape(title)}</title>
  {alternate}
  <style>{_page_styles()}</style>
</head>
<body>
  <main>
    <header>
      <p class="eyebrow">YouTube Finance Digest</p>
      <h1>{_escape(title)}</h1>
      <div class="meta">
        <span class="pill">{_escape(digest.get("published") or "发布时间未知")}</span>
        <span class="pill">内容来源：{_escape(source_status)}</span>
        <span class="pill">摘要状态：{_escape(summary_status)}</span>
      </div>
      <div class="actions">
        <a class="button" href="{_escape(original_url)}" rel="noopener noreferrer">打开原视频</a>
        <a class="button" href="{_escape(feed_url)}">RSS feed</a>
      </div>
    </header>
    <article>
      {summary_html}
    </article>
    <footer>
      该摘要仅用于信息整理，不构成投资建议。
    </footer>
  </main>
</body>
</html>
"""


def _render_index_page(config: Dict[str, Any], items: List[Dict[str, Any]]) -> str:
    rss_config = config.get("rss", {})
    title = rss_config.get("title", "Daily YouTube Finance Digest")
    feed_url = _feed_url(config)

    rows = []
    for digest in sorted(items, key=_sort_key, reverse=True):
        rows.append(
            "<li>"
            f"<a href=\"{_escape(item_page_url(config, digest))}\">{_escape(digest.get('title'))}</a>"
            f"<br><small>{_escape(digest.get('channel_name'))} · {_escape(digest.get('published') or '')} · "
            f"{_escape(digest.get('source_status') or 'link-only')}</small>"
            "</li>"
        )
    rows_html = "\n".join(rows) if rows else "<li>暂无摘要。</li>"

    alternate = ""
    if feed_url:
        alternate = (
            f'<link rel="alternate" type="application/rss+xml" '
            f'title="{_escape(title)}" href="{_escape(feed_url)}">'
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_escape(title)}</title>
  {alternate}
  <style>{_page_styles()}</style>
</head>
<body>
  <main>
    <header>
      <p class="eyebrow">RSS Digest</p>
      <h1>{_escape(title)}</h1>
      <p>{_escape(rss_config.get("description", ""))}</p>
      <div class="actions">
        <a class="button" href="{_escape(feed_url)}">订阅 feed.xml</a>
      </div>
    </header>
    <article>
      <h2>最新摘要</h2>
      <ul>{rows_html}</ul>
    </article>
  </main>
</body>
</html>
"""


def write_site_pages(config: Dict[str, Any], items: List[Dict[str, Any]], root_dir: Path) -> List[Path]:
    output_path = root_dir / config.get("rss", {}).get("output_path", "public/feed.xml")
    public_dir = output_path.parent
    items_dir = public_dir / "items"
    items_dir.mkdir(parents=True, exist_ok=True)

    written: List[Path] = []
    for digest in items:
        page_path = items_dir / f"{_safe_video_id(digest.get('video_id'))}.html"
        page_path.write_text(_render_item_page(config, digest), encoding="utf-8")
        written.append(page_path)

    index_path = public_dir / "index.html"
    index_path.write_text(_render_index_page(config, items), encoding="utf-8")
    written.append(index_path)
    return written


def build_rss(config: Dict[str, Any], items: List[Dict[str, Any]]) -> ET.ElementTree:
    rss_config = config.get("rss", {})
    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = rss_config.get("title", "Daily YouTube Finance Digest")
    ET.SubElement(channel, "link").text = _site_url(config) or rss_config.get("link", "")
    ET.SubElement(channel, "description").text = rss_config.get("description", "")
    ET.SubElement(channel, "language").text = config.get("summary", {}).get("language", "zh-CN")
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    feed_url = _feed_url(config)
    if feed_url:
        ET.SubElement(channel, f"{{{ATOM_NS}}}link", {"href": feed_url, "rel": "self", "type": "application/rss+xml"})

    for digest in sorted(items, key=_sort_key, reverse=True):
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = f"[{digest.get('channel_name')}] {digest.get('title')}"
        ET.SubElement(item, "link").text = item_page_url(config, digest)
        guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
        guid.text = digest.get("video_id")
        ET.SubElement(item, "pubDate").text = _rss_date(digest.get("published") or digest.get("processed_at"))
        ET.SubElement(item, "description").text = digest.get("summary_html", "")
        ET.SubElement(item, f"{{{CONTENT_NS}}}encoded").text = digest.get("summary_html", "")
        for category in ["YouTube Digest", "Finance", "Stock Market", digest.get("source_status", "link-only")]:
            ET.SubElement(item, "category").text = category

    return ET.ElementTree(root)


def write_rss(config: Dict[str, Any], items: List[Dict[str, Any]], root_dir: Path) -> Path:
    output_path = root_dir / config.get("rss", {}).get("output_path", "public/feed.xml")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_site_pages(config, items, root_dir)
    tree = build_rss(config, items)
    ET.indent(tree, space="  ")
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    return output_path
