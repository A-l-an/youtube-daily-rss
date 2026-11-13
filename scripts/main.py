from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

from asr import ASRResult, atomic_json, cache_root, read_checkpoint, transcribe_video
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
    item = find_daily_summary(summaries, digest_id)
    return bool(item and digest_complete(item))


def find_daily_summary(summaries: Dict[str, Any], digest_id: str) -> Optional[Dict[str, Any]]:
    return next((item for item in summaries.get("items", [])
                 if (item.get("digest_id") or item.get("video_id")) == digest_id), None)


def source_available(record: Dict[str, Any]) -> bool:
    if "text" in record:
        return source_has_text(record)
    return (record.get("source_status") == "captions" and record.get("transcript_status") == "success"
            or record.get("source_status") == "ASR" and record.get("asr_status") == "success")


def source_has_text(record: Dict[str, Any]) -> bool:
    return bool(str(record.get("text") or "").strip())


def digest_complete(item: Dict[str, Any]) -> bool:
    sources = item.get("source_videos") or []
    return bool(item.get("completion_status") != "pending" and item.get("summary_status") == "success"
                and sources and all(source_available(source) for source in sources))


def original_videos(item: Dict[str, Any]) -> List[Video]:
    return [Video(**{key: source[key] for key in Video.__dataclass_fields__})
            for source in item["source_videos"]]


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
            "text_sha256",
            "retry_error",
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
        "coverage_status": "complete" if records and all(source_has_text(record) for record in records) else "pending",
    }
    item["completion_status"] = "complete" if item["coverage_status"] == "complete" and summary_result.status == "success" else "pending"
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
        "last_attempted_at": processed_at,
        "source_video_ids": [record.get("video_id") for record in records],
        "summary_status": summary_result.status,
        "summary_model": getattr(summary_result, "model", None),
        "source_status": combined_source_status(records),
        "coverage_status": "complete" if records and all(source_has_text(record) for record in records) else "pending",
    }
    daily_digest["completion_status"] = "complete" if daily_digest["coverage_status"] == "complete" and summary_result.status == "success" else "pending"
    if report_date is not None:
        daily_digest["report_date"] = report_date.isoformat()
    state["daily_digests"][digest_id] = daily_digest


def _source_cache_path(video: Video, config: Dict[str, Any]) -> Path:
    return cache_root(config.get("asr", {})) / "sources" / (hashlib.sha256(video.url.encode()).hexdigest() + ".json")


def _cached_source(video: Video, config: Dict[str, Any]) -> Dict[str, Any]:
    record = read_checkpoint(_source_cache_path(video, config))
    text = record.get("text")
    if (all(record.get(key) == value for key, value in video.to_dict().items())
            and isinstance(text, str) and text.strip()
            and record.get("text_sha256") == hashlib.sha256(text.encode()).hexdigest()):
        return record
    return {}


def _summary_from_item(item: Dict[str, Any]) -> SummaryResult:
    body = re.sub(r"<!-- coverage -->.*?<!-- /coverage -->", "", item.get("summary_html") or "", flags=re.S)
    return SummaryResult(status=item["summary_status"], summary_html=body,
                         model=item.get("summary_model"), error_message=item.get("summary_error"),
                         summary_data=item.get("summary_data"))


def _input_hash(records: List[Dict[str, Any]]) -> str:
    fields = [*Video.__dataclass_fields__, "source_status", "content_source", "text"]
    inputs = [{key: record.get(key) for key in fields} for record in records]
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _public_source_failure(error: Any) -> str:
    diagnostic = str(error or "").lower()
    if any(code in diagnostic for code in ("remote_busy", "cache_busy")):
        return "转录服务繁忙，稍后重试"
    if any(code in diagnostic for code in ("remote_unavailable", "ssh_unavailable", "cli_unavailable")):
        return "转录服务暂不可用，稍后重试"
    if "timeout" in diagnostic or "timed out" in diagnostic:
        return "转录请求超时，稍后重试"
    if any(code in diagnostic for code in ("members-only", "members only", "private video", "access denied", "forbidden", "sign in", "403")):
        return "视频访问受限，需确认访问权限"
    if "download" in diagnostic or "yt-dlp" in diagnostic:
        return "暂时无法获取视频音频，稍后重试"
    return "暂未取得可用正文，稍后重试"


def _process_digest(digest_id: str, videos: List[Video], existing: Optional[Dict[str, Any]],
                    config: Dict[str, Any], state: Dict[str, Any], summaries: Dict[str, Any], *,
                    dry_run: bool, force: bool, report_date: Optional[date], older: bool = False) -> bool:
    records = []
    previous_sources = {source["video_id"]: source for source in (existing or {}).get("source_videos", [])}
    for video in videos:
        record = {} if dry_run or force else _cached_source(video, config)
        if not record:
            try:
                record = collect_video_content(video, config, dry_run=dry_run)
            except Exception as exc:
                logging.exception("[%s] unexpected processing failure: %s", video.channel_name, exc)
                record = failed_video_record(video, exc)
            if source_available(record) and not dry_run:
                record["text_sha256"] = hashlib.sha256(record["text"].encode()).hexdigest()
                atomic_json(_source_cache_path(video, config), record)
            elif not dry_run:
                cached = _cached_source(video, config)
                previous = previous_sources.get(video.video_id, {})
                if cached:
                    record = cached
                elif source_available(previous):
                    record = {**previous, "retry_error": record.get("asr_error") or record.get("transcript_error")}
        records.append(record)
    if dry_run:
        logging.info("dry-run complete; digest=%s text_sources=%d/%d; no files were modified",
                     digest_id, sum(source_available(record) for record in records), len(records))
        return False

    text_complete = all(source_has_text(record) for record in records)
    if existing and digest_complete(existing) and not text_complete:
        logging.warning("[daily] failed forced retry; retained complete digest %s", digest_id)
        return False
    fingerprint = _input_hash(records)
    unchanged = bool(existing and existing.get("summary_attempt_input_hash", existing.get("summary_input_hash")) == fingerprint)
    # Legacy items did not retain text hashes; an unchanged missing-source set keeps their useful analysis.
    legacy_unchanged = bool(existing and "summary_attempt_input_hash" not in existing and not existing.get("summary_input_hash")
                            and existing.get("summary_status") == "success"
                            and all(source_available(record) == source_available(previous_sources.get(record["video_id"], {}))
                                    for record in records))
    retain = bool(existing and not force
                  and existing.get("summary_status") == "success"
                  and existing.get("summary_attempt_status", "success") == "success"
                  and (not text_complete and (unchanged or legacy_unchanged)
                       or any(source_available(record) and not record.get("text") for record in records)))
    result = _summary_from_item(existing) if retain else summarize_daily_digest(records, config.get("summary", {}))
    attempt_status = existing.get("summary_attempt_status", result.status) if retain else result.status
    attempt_error = existing.get("summary_attempt_error", result.error_message) if retain else result.error_message
    summary_current = not retain or existing.get("summary_input_hash") == fingerprint or legacy_unchanged
    if existing and result.status != "success" and existing.get("summary_status") == "success":
        if digest_complete(existing):
            logging.warning("[daily] failed forced summary retry; retained complete digest %s", digest_id)
            return False
        result = _summary_from_item(existing)
        summary_current = False
    published = (existing or {}).get("published") or (report_date_published_iso(report_date, config) if report_date else None)
    item = build_daily_item(digest_id, records, result, config, report_date=report_date, published=published)
    if existing:
        item["video_id"] = existing.get("video_id") or digest_id
    item["summary_input_hash"] = fingerprint if summary_current else (existing or {}).get("summary_input_hash")
    item["summary_attempt_input_hash"] = fingerprint
    item["summary_attempt_status"] = attempt_status
    item["summary_attempt_error"] = attempt_error
    if not summary_current:
        item["completion_status"] = "pending"
    if item["completion_status"] == "pending":
        details = []
        for record in records:
            if not source_has_text(record) or record.get("retry_error"):
                reason = record.get("retry_error") or record.get("asr_error") or record.get("transcript_error") or "source text unavailable"
                details.append(f"{record.get('channel_name')}：{_public_source_failure(reason)}。")
        if attempt_status != "success":
            details.append("本次摘要更新失败，已保留已有分析，稍后重试。" if result.status == "success"
                           else "摘要暂未生成，稍后重试。")
        elif not summary_current:
            details.append("已有摘要已保留，等待完整原文后更新。")
        coverage = "正文已齐" if item["coverage_status"] == "complete" else "正文待补齐"
        status = f"{coverage}（{sum(source_has_text(record) for record in records)}/{len(records)}）；摘要待更新。"
        item["summary_html"] += "<!-- coverage --><p>" + html.escape(status + " ".join(details)) + "</p><!-- /coverage -->"
    upsert_daily_summary(summaries, item)
    mark_state_processed(state, digest_id, records, result, item["processed_at"], report_date=report_date,
                         preserve_existing_video_links=report_date is not None or older)
    state["daily_digests"][digest_id].update({key: item[key] for key in ("coverage_status", "completion_status")})
    state["last_run_at"] = item["processed_at"]
    logging.info("[daily] digest=%s coverage=%s completion=%s summary=%s", digest_id,
                 item["coverage_status"], item["completion_status"], item["summary_status"])
    return True


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
    dated_existing = find_daily_summary(summaries, digest_id) if digest_id else None
    if dated_existing and dated_existing.get("source_videos"):
        latest_videos = original_videos(dated_existing)
    for channel in ([] if latest_videos else channels):
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
    selection_complete = not fetch_failed and len(latest_videos) == len(channels)
    should_process = selection_complete and (dry_run or force or not daily_exists)
    if not selection_complete:
        logging.error("Incomplete channel selection; no new daily digest will be created")

    logging.info(
        "latest daily digest candidate=%s videos=%s daily_exists=%s new_video_ids=%s",
        digest_id,
        ", ".join(video.video_id for video in latest_videos),
        daily_exists,
        ", ".join(new_video_ids) or "none",
    )

    existing = find_daily_summary(summaries, digest_id)
    if existing and existing.get("source_videos"):
        latest_videos = original_videos(existing)
    candidates = sorted(summaries.get("items", []), key=lambda item: item.get("published") or "", reverse=True)
    older_candidates = [item for index, item in enumerate(candidates)
                       if (item.get("digest_id") or item.get("video_id")) != digest_id
                       and not digest_complete(item)
                       and (item.get("completion_status") == "pending"
                            or state["daily_digests"].get(item.get("digest_id") or item.get("video_id"), {}).get("completion_status") == "pending"
                            or index == 0)] if target_report_date is None and not dry_run else []
    older_item = min(older_candidates, key=lambda item: (
        state["daily_digests"].get(item.get("digest_id") or item.get("video_id"), {}).get("last_attempted_at")
        or item.get("processed_at") or ""), default=None)
    changed = False
    if should_process:
        changed = _process_digest(digest_id, latest_videos, existing, config, state, summaries,
                                  dry_run=dry_run, force=force, report_date=target_report_date)
    if older_item:
        older_id = older_item.get("digest_id") or older_item["video_id"]
        state["daily_digests"].setdefault(older_id, {}).update(
            last_attempted_at=utc_now_iso(), completion_status="pending")
        changed = True
        try:
            old_videos = original_videos(older_item)
            old_date = date.fromisoformat(older_item["report_date"]) if older_item.get("report_date") else None
        except (KeyError, TypeError, ValueError):
            logging.error("Cannot reconstruct original videos for pending digest %s", older_id)
        else:
            changed = _process_digest(older_id, old_videos, older_item, config, state, summaries,
                                      dry_run=False, force=False, report_date=old_date, older=True) or changed
    if changed:
        save_json(STATE_PATH, state)
        save_json(SUMMARIES_PATH, summaries)
        feed_path = write_rss(config, summaries.get("items", []), ROOT)
        logging.info("RSS feed generated: %s", feed_path)
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
