from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

from asr import ASRResult, transcribe_video
from fetch_youtube import Video, fetch_latest_video, fetch_video_for_report_date
from rss import write_rss
from summarize import SummaryResult, retitle_rendered_daily_summary, summarize_daily_digest
from transcript import TranscriptResult, get_transcript


ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "state.json"
SUMMARIES_PATH = ROOT / "summaries.json"
CONFIG_PATH = ROOT / "config.yml"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def load_config() -> Dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def ensure_state_shape(state: Dict[str, Any]) -> Dict[str, Any]:
    state.setdefault("last_run_at", None)
    state.setdefault("processed_videos", {})
    state.setdefault("daily_digests", {})
    return state


def ensure_summaries_shape(summaries: Dict[str, Any]) -> Dict[str, Any]:
    items = summaries.setdefault("items", [])
    legacy_items = summaries.setdefault("legacy_video_items", [])
    daily_items: List[Dict[str, Any]] = []
    legacy_ids = {item.get("video_id") for item in legacy_items}

    for item in items:
        video_id = str(item.get("video_id") or "")
        if item.get("item_type") == "daily_digest" or item.get("digest_id") or video_id.startswith("daily-"):
            item["item_type"] = "daily_digest"
            daily_items.append(item)
            continue
        if item.get("video_id") not in legacy_ids:
            legacy_items.append(item)
            legacy_ids.add(item.get("video_id"))

    summaries["items"] = daily_items
    return summaries


def upsert_daily_summary(summaries: Dict[str, Any], item: Dict[str, Any]) -> None:
    digest_id = item.get("digest_id") or item.get("video_id")
    existing = [
        old
        for old in summaries.get("items", [])
        if (old.get("digest_id") or old.get("video_id")) != digest_id
    ]
    existing.append(item)
    summaries["items"] = existing


def source_from_results(transcript_result: TranscriptResult, asr_result: ASRResult) -> str:
    if transcript_result.status == "success":
        return "captions"
    if asr_result.status == "success":
        return "ASR"
    return "link-only"


def content_source(source_status: str) -> str:
    if source_status == "captions":
        return "YouTube captions"
    if source_status == "ASR":
        return "ASR transcription"
    return "metadata only"


def local_date_label(config: Dict[str, Any]) -> str:
    timezone_name = config.get("timezone", "Asia/Shanghai")
    try:
        tzinfo = ZoneInfo(timezone_name)
    except Exception:
        tzinfo = timezone.utc
    return datetime.now(timezone.utc).astimezone(tzinfo).strftime("%Y-%m-%d")


def configured_timezone(config: Dict[str, Any]) -> ZoneInfo:
    timezone_name = str(config.get("timezone") or "Asia/Shanghai")
    try:
        return ZoneInfo(timezone_name)
    except Exception as exc:
        raise ValueError(f"Invalid configured timezone: {timezone_name!r}") from exc


def parse_ended_report_date(
    value: str,
    config: Dict[str, Any],
    now: Optional[datetime] = None,
) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""):
        raise ValueError("--report-date must use YYYY-MM-DD format")
    try:
        report_date = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid --report-date: {value!r}") from exc

    local_timezone = configured_timezone(config)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_today = current.astimezone(local_timezone).date()
    if report_date >= local_today:
        raise ValueError(
            f"--report-date must be an ended local date before {local_today.isoformat()} "
            f"in {local_timezone.key}"
        )
    return report_date


def report_date_published_iso(report_date: date, config: Dict[str, Any]) -> str:
    cutoff_local = datetime.combine(
        report_date + timedelta(days=1),
        time.min,
        tzinfo=configured_timezone(config),
    )
    return (cutoff_local - timedelta(microseconds=1)).astimezone(timezone.utc).isoformat()


def report_date_digest_id(report_date: date) -> str:
    return f"daily-date-{report_date.isoformat()}"


REPORT_DATE_TITLE_PREFIX = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s*(?:[-–—:：|]\s*)?")


def title_for_report_date(report_date: date, title: Any) -> str:
    title_without_date = REPORT_DATE_TITLE_PREFIX.sub("", str(title or "")).strip()
    return f"{report_date.isoformat()} {title_without_date or '美股视频融合摘要'}"


def daily_digest_id(videos: List[Video]) -> str:
    video_ids = [video.video_id for video in videos if video.video_id]
    return "daily-" + "-".join(video_ids) if video_ids else "daily-empty"


def has_daily_summary(summaries: Dict[str, Any], digest_id: str) -> bool:
    return any((item.get("digest_id") or item.get("video_id")) == digest_id for item in summaries.get("items", []))


def combined_source_status(records: List[Dict[str, Any]]) -> str:
    statuses = sorted({str(record.get("source_status") or "link-only") for record in records})
    if not statuses:
        return "link-only"
    if len(statuses) == 1:
        return statuses[0]
    return "mixed"


def combined_content_source(records: List[Dict[str, Any]]) -> str:
    sources = sorted({str(record.get("content_source") or "metadata only") for record in records})
    return " + ".join(sources) if sources else "metadata only"


def collect_video_content(
    video: Video,
    config: Dict[str, Any],
    dry_run: bool,
) -> Dict[str, Any]:
    video_dict = video.to_dict()
    logging.info("[%s] detected latest video: %s (%s)", video.channel_name, video.title, video.video_id)

    transcript_result = get_transcript(video.video_id, config.get("transcript", {}))
    logging.info(
        "[%s] transcript status=%s language=%s source=%s error=%s",
        video.channel_name,
        transcript_result.status,
        transcript_result.language,
        transcript_result.source,
        transcript_result.error_message,
    )

    asr_result = ASRResult(status="skipped", error_message="Transcript succeeded or dry-run skipped ASR")
    text = transcript_result.text if transcript_result.status == "success" else ""
    if not text and not dry_run:
        asr_result = transcribe_video(video.url, config.get("asr", {}))
        logging.info(
            "[%s] ASR status=%s model=%s error=%s",
            video.channel_name,
            asr_result.status,
            asr_result.model,
            asr_result.error_message,
        )
        text = asr_result.text if asr_result.status == "success" else ""
    elif not text and dry_run:
        logging.info("[%s] dry-run: ASR is not attempted", video.channel_name)

    source_status = source_from_results(transcript_result, asr_result)
    if dry_run:
        preview = text[:1200].replace("\n", " ") if text else "(no transcript text available)"
        logging.info("[%s] dry-run source_status=%s; text preview: %s", video.channel_name, source_status, preview)

    return {
        **video_dict,
        "source_status": source_status,
        "content_source": content_source(source_status),
        "transcript_status": transcript_result.status,
        "transcript_language": transcript_result.language,
        "transcript_source": transcript_result.source,
        "transcript_error": transcript_result.error_message,
        "asr_status": asr_result.status,
        "asr_model": asr_result.model,
        "asr_error": asr_result.error_message,
        "text": text,
    }


def failed_video_record(video: Video, exc: Exception) -> Dict[str, Any]:
    video_dict = video.to_dict()
    message = str(exc)
    return {
        **video_dict,
        "source_status": "link-only",
        "content_source": "metadata only",
        "transcript_status": "failed",
        "transcript_language": None,
        "transcript_source": "unknown",
        "transcript_error": message,
        "asr_status": "failed",
        "asr_model": None,
        "asr_error": message,
        "text": "",
    }


def source_video_snapshot(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key
        in {
            "channel_name",
            "channel_id",
            "handle",
            "video_id",
            "title",
            "url",
            "published",
            "source_status",
            "content_source",
            "transcript_status",
            "transcript_language",
            "transcript_source",
            "transcript_error",
            "asr_status",
            "asr_model",
            "asr_error",
        }
    }


def build_daily_item(
    digest_id: str,
    records: List[Dict[str, Any]],
    summary_result: SummaryResult,
    config: Dict[str, Any],
    report_date: Optional[date] = None,
    published: Optional[str] = None,
) -> Dict[str, Any]:
    processed_at = utc_now_iso()
    summary_data = getattr(summary_result, "summary_data", None) or {}
    title = summary_data.get("title") or f"{local_date_label(config)} 美股视频融合摘要"
    if report_date is not None:
        title = title_for_report_date(report_date, title)
        summary_result = retitle_rendered_daily_summary(summary_result, title)
        summary_data = getattr(summary_result, "summary_data", None) or {}
    source_videos = [source_video_snapshot(record) for record in records]

    item = {
        "item_type": "daily_digest",
        "digest_id": digest_id,
        "video_id": digest_id,
        "source_video_ids": [record.get("video_id") for record in records],
        "channel_name": "视野环球财经 + NaNa说美股",
        "title": title,
        "url": "",
        "published": published or processed_at,
        "processed_at": processed_at,
        "source_status": combined_source_status(records),
        "content_source": combined_content_source(records),
        "summary_status": summary_result.status,
        "summary_model": getattr(summary_result, "model", None),
        "summary_error": getattr(summary_result, "error_message", None),
        "summary_data": summary_data or None,
        "source_videos": source_videos,
        "summary_html": summary_result.summary_html,
    }
    if report_date is not None:
        item["report_date"] = report_date.isoformat()
    return item


def mark_state_processed(
    state: Dict[str, Any],
    digest_id: str,
    records: List[Dict[str, Any]],
    summary_result: SummaryResult,
    processed_at: str,
    report_date: Optional[date] = None,
    preserve_existing_video_links: bool = False,
) -> None:
    for record in records:
        video_id = record.get("video_id")
        if not video_id:
            continue
        if preserve_existing_video_links and video_id in state["processed_videos"]:
            continue
        processed_video = {
            "channel_name": record.get("channel_name"),
            "channel_id": record.get("channel_id"),
            "title": record.get("title"),
            "url": record.get("url"),
            "published": record.get("published"),
            "processed_at": processed_at,
            "daily_digest_id": digest_id,
            "transcript_status": record.get("transcript_status"),
            "transcript_language": record.get("transcript_language"),
            "transcript_source": record.get("transcript_source"),
            "asr_status": record.get("asr_status"),
            "summary_status": summary_result.status,
            "source_status": record.get("source_status"),
        }
        if report_date is not None:
            processed_video["report_date"] = report_date.isoformat()
        state["processed_videos"][video_id] = processed_video

    daily_digest = {
        "processed_at": processed_at,
        "source_video_ids": [record.get("video_id") for record in records],
        "summary_status": summary_result.status,
        "summary_model": getattr(summary_result, "model", None),
        "source_status": combined_source_status(records),
    }
    if report_date is not None:
        daily_digest["report_date"] = report_date.isoformat()
    state["daily_digests"][digest_id] = daily_digest


def run(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env")
    config = load_config()
    dry_run = bool(getattr(args, "dry_run", False))
    force = bool(getattr(args, "force", False))
    report_date_value = getattr(args, "report_date", None)
    target_report_date: Optional[date] = None
    if report_date_value:
        try:
            target_report_date = parse_ended_report_date(str(report_date_value), config)
        except ValueError as exc:
            logging.error("Invalid report date: %s; no files were modified", exc)
            return 2

    try:
        import ytdlp_opts

        ytdlp_opts.bootstrap_env_from_config(config)
    except Exception as exc:
        logging.warning("youtube_access env bootstrap skipped: %s", exc)
    state = ensure_state_shape(load_json(STATE_PATH, {"last_run_at": None, "processed_videos": {}, "daily_digests": {}}))
    summaries = ensure_summaries_shape(load_json(SUMMARIES_PATH, {"items": []}))

    digest_id = report_date_digest_id(target_report_date) if target_report_date is not None else None
    if digest_id and has_daily_summary(summaries, digest_id) and not force and not dry_run:
        logging.info("skip report-date digest %s; it already exists and no files were modified", digest_id)
        return 0

    latest_videos: List[Video] = []
    channels = config.get("channels", []) or []
    fetch_failed = False
    for channel in channels:
        try:
            if target_report_date is not None:
                video = fetch_video_for_report_date(
                    channel,
                    target_report_date,
                    str(config.get("timezone") or "Asia/Shanghai"),
                )
            else:
                video = fetch_latest_video(channel)
            latest_videos.append(video)
        except Exception as exc:
            logging.exception("[%s] failed to fetch latest video: %s", channel.get("name", "unknown"), exc)
            fetch_failed = True
            continue

    if target_report_date is not None and (fetch_failed or not channels or len(latest_videos) != len(channels)):
        logging.error(
            "report-date selection failed for %s; every channel needs one trustworthy Atom entry; no files were modified",
            target_report_date.isoformat(),
        )
        return 1

    if not latest_videos:
        logging.error("No latest videos were fetched; keeping existing RSS, summaries, and state unchanged")
        return 0

    digest_id = digest_id or daily_digest_id(latest_videos)
    daily_exists = has_daily_summary(summaries, digest_id)
    new_video_ids = [
        video.video_id
        for video in latest_videos
        if video.video_id not in state.get("processed_videos", {})
    ]
    should_process = dry_run or force or not daily_exists or bool(new_video_ids)

    logging.info(
        "latest daily digest candidate=%s videos=%s daily_exists=%s new_video_ids=%s",
        digest_id,
        ", ".join(video.video_id for video in latest_videos),
        daily_exists,
        ", ".join(new_video_ids) or "none",
    )

    if not should_process:
        logging.info("skip daily digest; latest video set already has a merged RSS item; no files were modified")
        return 0

    records: List[Dict[str, Any]] = []
    for video in latest_videos:
        try:
            records.append(collect_video_content(video, config, dry_run=dry_run))
        except Exception as exc:
            logging.exception("[%s] unexpected processing failure: %s", video.channel_name, exc)
            records.append(failed_video_record(video, exc))

    if dry_run:
        source_status = combined_source_status(records)
        available_text = sum(1 for record in records if str(record.get("text") or "").strip())
        logging.info(
            "dry-run complete; would generate one merged daily digest %s source_status=%s text_sources=%d/%d; no files were modified",
            digest_id,
            source_status,
            available_text,
            len(records),
        )
        return 0

    summary_result = summarize_daily_digest(records, config.get("summary", {}))
    logging.info(
        "[daily] summary status=%s model=%s error=%s",
        summary_result.status,
        getattr(summary_result, "model", None),
        getattr(summary_result, "error_message", None),
    )

    daily_item = build_daily_item(
        digest_id,
        records,
        summary_result,
        config,
        report_date=target_report_date,
        published=(
            report_date_published_iso(target_report_date, config)
            if target_report_date is not None
            else None
        ),
    )
    upsert_daily_summary(summaries, daily_item)
    mark_state_processed(
        state,
        digest_id,
        records,
        summary_result,
        daily_item["processed_at"],
        report_date=target_report_date,
        preserve_existing_video_links=target_report_date is not None,
    )
    logging.info("[daily] RSS item prepared digest_id=%s source_status=%s", digest_id, daily_item["source_status"])

    state["last_run_at"] = daily_item["processed_at"]
    save_json(STATE_PATH, state)
    save_json(SUMMARIES_PATH, summaries)
    feed_path = write_rss(config, summaries.get("items", []), ROOT)
    logging.info("RSS feed generated: %s (1 new/updated daily item)", feed_path)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build daily YouTube finance digest RSS feed")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and inspect latest videos without writing state or RSS")
    parser.add_argument("--force", action="store_true", help="Reprocess the latest video from each channel")
    parser.add_argument(
        "--report-date",
        metavar="YYYY-MM-DD",
        help="Build one idempotent digest for an ended local date using only trusted Atom timestamps",
    )
    parser.add_argument("--log-level", default="INFO", help="Python logging level")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
