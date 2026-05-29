from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv

from asr import ASRResult, transcribe_video
from fetch_youtube import Video, fetch_latest_video
from rss import write_rss
from summarize import SummaryResult, render_link_only_digest, summarize_transcript
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
    return state


def ensure_summaries_shape(summaries: Dict[str, Any]) -> Dict[str, Any]:
    summaries.setdefault("items", [])
    return summaries


def upsert_summary(summaries: Dict[str, Any], item: Dict[str, Any]) -> None:
    existing = [old for old in summaries.get("items", []) if old.get("video_id") != item.get("video_id")]
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


def process_video(
    video: Video,
    config: Dict[str, Any],
    state: Dict[str, Any],
    summaries: Dict[str, Any],
    dry_run: bool,
) -> Optional[Dict[str, Any]]:
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
        return None

    if text:
        summary_result = summarize_transcript(video_dict, text, content_source(source_status), config.get("summary", {}))
    else:
        summary_result = SummaryResult(status="link_only", summary_html=render_link_only_digest(video_dict))

    logging.info(
        "[%s] summary status=%s model=%s error=%s",
        video.channel_name,
        summary_result.status,
        getattr(summary_result, "model", None),
        getattr(summary_result, "error_message", None),
    )

    processed_at = utc_now_iso()
    item = {
        **video_dict,
        "processed_at": processed_at,
        "source_status": source_status,
        "content_source": content_source(source_status),
        "summary_status": summary_result.status,
        "summary_model": getattr(summary_result, "model", None),
        "summary_error": getattr(summary_result, "error_message", None),
        "transcript_status": transcript_result.status,
        "transcript_language": transcript_result.language,
        "transcript_source": transcript_result.source,
        "transcript_error": transcript_result.error_message,
        "asr_status": asr_result.status,
        "asr_model": asr_result.model,
        "asr_error": asr_result.error_message,
        "summary_html": summary_result.summary_html,
    }

    upsert_summary(summaries, item)
    state["processed_videos"][video.video_id] = {
        "channel_name": video.channel_name,
        "channel_id": video.channel_id,
        "title": video.title,
        "url": video.url,
        "published": video.published,
        "processed_at": processed_at,
        "transcript_status": transcript_result.status,
        "transcript_language": transcript_result.language,
        "transcript_source": transcript_result.source,
        "asr_status": asr_result.status,
        "summary_status": summary_result.status,
        "source_status": source_status,
    }
    logging.info("[%s] RSS item prepared source_status=%s", video.channel_name, source_status)
    return item


def run(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env")
    config = load_config()
    state = ensure_state_shape(load_json(STATE_PATH, {"last_run_at": None, "processed_videos": {}}))
    summaries = ensure_summaries_shape(load_json(SUMMARIES_PATH, {"items": []}))

    processed_count = 0
    for channel in config.get("channels", []):
        try:
            video = fetch_latest_video(channel)
        except Exception as exc:
            logging.exception("[%s] failed to fetch latest video: %s", channel.get("name", "unknown"), exc)
            continue

        already_processed = video.video_id in state.get("processed_videos", {})
        if already_processed and not args.force:
            logging.info("[%s] skip already processed video: %s", video.channel_name, video.video_id)
            continue

        try:
            item = process_video(video, config, state, summaries, dry_run=args.dry_run)
            if item is not None:
                processed_count += 1
        except Exception as exc:
            logging.exception("[%s] unexpected processing failure: %s", video.channel_name, exc)
            if not args.dry_run:
                video_dict = video.to_dict()
                processed_at = utc_now_iso()
                item = {
                    **video_dict,
                    "processed_at": processed_at,
                    "source_status": "link-only",
                    "content_source": "metadata only",
                    "summary_status": "failed",
                    "summary_error": str(exc),
                    "summary_html": render_link_only_digest(video_dict, f"Processing failed: {exc}. The original video link is provided."),
                }
                upsert_summary(summaries, item)
                state["processed_videos"][video.video_id] = {
                    "channel_name": video.channel_name,
                    "channel_id": video.channel_id,
                    "title": video.title,
                    "url": video.url,
                    "published": video.published,
                    "processed_at": processed_at,
                    "transcript_status": "failed",
                    "asr_status": "failed",
                    "summary_status": "failed",
                    "source_status": "link-only",
                }
                processed_count += 1

    if args.dry_run:
        logging.info("dry-run complete; no files were modified")
        return 0

    state["last_run_at"] = utc_now_iso()
    save_json(STATE_PATH, state)
    save_json(SUMMARIES_PATH, summaries)
    feed_path = write_rss(config, summaries.get("items", []), ROOT)
    logging.info("RSS feed generated: %s (%d new/updated item(s))", feed_path, processed_count)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build daily YouTube finance digest RSS feed")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and inspect latest videos without writing state or RSS")
    parser.add_argument("--force", action="store_true", help="Reprocess the latest video from each channel")
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
