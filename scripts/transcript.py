from __future__ import annotations

from dataclasses import dataclass, asdict
import html
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass
class TranscriptResult:
    status: str
    language: Optional[str] = None
    source: str = "unknown"
    text: str = ""
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _list_transcripts(video_id: str) -> Iterable[Any]:
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    if hasattr(api, "list"):
        return api.list(video_id)
    return YouTubeTranscriptApi.list_transcripts(video_id)


def _is_unavailable_exception(exc: Exception) -> bool:
    try:
        from youtube_transcript_api import _errors

        unavailable_types = (
            getattr(_errors, "TranscriptsDisabled"),
            getattr(_errors, "NoTranscriptFound"),
        )
        return isinstance(exc, unavailable_types)
    except Exception:
        message = str(exc).lower()
        return "subtitles are disabled" in message or "no transcript" in message


def _snippet_text(snippet: Any) -> str:
    if isinstance(snippet, dict):
        return snippet.get("text", "")
    return getattr(snippet, "text", "")


def _fetch_text(transcript: Any) -> str:
    fetched = transcript.fetch()
    snippets = getattr(fetched, "snippets", fetched)
    parts = [_snippet_text(snippet).strip() for snippet in snippets]
    return "\n".join(part for part in parts if part)


def _clean_subtitle_text(value: str) -> str:
    text = html.unescape(value)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\{\\[^}]+\}", "", text)
    return " ".join(text.split())


def _parse_vtt(path: Path) -> str:
    import webvtt

    captions = webvtt.read(str(path))
    parts = [_clean_subtitle_text(caption.text) for caption in captions]
    return "\n".join(part for part in parts if part)


def _parse_srt(path: Path) -> str:
    parts: List[str] = []
    block_lines: List[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            if block_lines:
                text_lines = [
                    item
                    for item in block_lines
                    if "-->" not in item and not item.strip().isdigit()
                ]
                text = _clean_subtitle_text(" ".join(text_lines))
                if text:
                    parts.append(text)
                block_lines = []
            continue
        block_lines.append(stripped)

    if block_lines:
        text_lines = [
            item
            for item in block_lines
            if "-->" not in item and not item.strip().isdigit()
        ]
        text = _clean_subtitle_text(" ".join(text_lines))
        if text:
            parts.append(text)

    return "\n".join(parts)


def _parse_subtitle_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".vtt":
        return _parse_vtt(path)
    if suffix == ".srt":
        return _parse_srt(path)
    raise RuntimeError(f"Unsupported subtitle format: {suffix or path.name}")


def _safe_filename_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "unknown"


def _download_yt_dlp_subtitle(
    video_id: str,
    language: str,
    generated: bool,
    subtitle_format: str,
    output_dir: Path,
) -> Path:
    import yt_dlp

    source = "auto" if generated else "manual"
    output_prefix = output_dir / f"subtitle-{source}-{_safe_filename_part(language)}"
    before = set(output_dir.glob("*"))
    options = {
        "skip_download": True,
        "writesubtitles": not generated,
        "writeautomaticsub": generated,
        "subtitleslangs": [language],
        "subtitlesformat": subtitle_format,
        "outtmpl": str(output_prefix) + ".%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extractor_args": {"youtube": {"player_client": ["android", "mweb", "web"]}},
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

    candidates = [
        path
        for path in sorted(output_dir.glob("*"))
        if path not in before and path.suffix.lower() in {".vtt", ".srt"}
    ]
    if not candidates:
        raise RuntimeError(f"yt-dlp did not produce {source} subtitles for {language}")
    return candidates[0]


def _candidate_score(
    transcript: Any,
    preferred_languages: List[str],
    allow_auto_captions: bool,
) -> Optional[Tuple[int, int]]:
    language = getattr(transcript, "language_code", None)
    if language not in preferred_languages:
        return None

    is_generated = bool(getattr(transcript, "is_generated", False))
    if is_generated and not allow_auto_captions:
        return None

    language_rank = preferred_languages.index(language)
    source_rank = 1 if is_generated else 0
    return language_rank, source_rank


def _yt_dlp_candidate_order(
    preferred_languages: List[str],
    allow_auto_captions: bool,
) -> List[Tuple[str, bool]]:
    candidates = [(language, False) for language in preferred_languages]
    if allow_auto_captions:
        candidates.extend((language, True) for language in preferred_languages)
    return candidates


def _get_yt_dlp_transcript(
    video_id: str,
    transcript_config: Dict[str, Any],
    preferred_languages: List[str],
    allow_auto_captions: bool,
) -> Optional[TranscriptResult]:
    fallback_config = transcript_config.get("yt_dlp_fallback", {})
    if not fallback_config.get("enabled", False):
        return None

    subtitle_format = str(fallback_config.get("subtitle_format", "vtt")).lower()
    sleep_interval = float(fallback_config.get("sleep_interval_seconds", 0) or 0)
    errors: List[str] = []

    with tempfile.TemporaryDirectory(prefix="youtube-rss-subs-") as tmp:
        output_dir = Path(tmp)
        for language, generated in _yt_dlp_candidate_order(preferred_languages, allow_auto_captions):
            try:
                subtitle_path = _download_yt_dlp_subtitle(
                    video_id,
                    language,
                    generated,
                    subtitle_format,
                    output_dir,
                )
                text = _parse_subtitle_file(subtitle_path)
            except Exception as exc:
                source = "auto" if generated else "manual"
                errors.append(f"{source} {language}: {exc}")
                if sleep_interval:
                    time.sleep(sleep_interval)
                continue

            if text.strip():
                return TranscriptResult(
                    status="success",
                    language=language,
                    source="yt_dlp_auto_caption" if generated else "yt_dlp_manual_subtitle",
                    text=text,
                )

            source = "auto" if generated else "manual"
            errors.append(f"{source} {language}: empty subtitle")
            if sleep_interval:
                time.sleep(sleep_interval)

    return TranscriptResult(
        status="failed",
        source="yt_dlp_subtitle",
        error_message="; ".join(errors) if errors else "yt-dlp subtitle fallback failed",
    )


def _return_with_yt_dlp_fallback(
    video_id: str,
    transcript_config: Dict[str, Any],
    preferred_languages: List[str],
    allow_auto_captions: bool,
    primary_result: TranscriptResult,
) -> TranscriptResult:
    fallback_result = _get_yt_dlp_transcript(
        video_id,
        transcript_config,
        preferred_languages,
        allow_auto_captions,
    )
    if fallback_result is None:
        return primary_result
    if fallback_result.status == "success":
        return fallback_result

    messages = [message for message in [primary_result.error_message, fallback_result.error_message] if message]
    primary_result.error_message = "; ".join(messages) if messages else primary_result.error_message
    return primary_result


def get_transcript(video_id: str, transcript_config: Dict[str, Any]) -> TranscriptResult:
    preferred_languages = transcript_config.get("preferred_languages") or ["zh-Hans", "zh", "zh-CN", "en"]
    allow_auto_captions = bool(transcript_config.get("allow_auto_captions", True))

    try:
        transcript_list = list(_list_transcripts(video_id))
    except Exception as exc:
        status = "unavailable" if _is_unavailable_exception(exc) else "failed"
        return _return_with_yt_dlp_fallback(
            video_id,
            transcript_config,
            preferred_languages,
            allow_auto_captions,
            TranscriptResult(status=status, error_message=str(exc)),
        )

    ranked: List[Tuple[Tuple[int, int], Any]] = []
    for transcript in transcript_list:
        score = _candidate_score(transcript, preferred_languages, allow_auto_captions)
        if score is not None:
            ranked.append((score, transcript))

    if not ranked:
        available = sorted({getattr(t, "language_code", "unknown") for t in transcript_list})
        return _return_with_yt_dlp_fallback(
            video_id,
            transcript_config,
            preferred_languages,
            allow_auto_captions,
            TranscriptResult(
                status="unavailable",
                error_message=f"No matching transcript languages. Available: {', '.join(available) or 'none'}",
            ),
        )

    best_result: Optional[TranscriptResult] = None
    best_score: Optional[Tuple[int, int]] = None
    errors: List[str] = []

    for score, candidate in sorted(ranked, key=lambda item: item[0]):
        if best_score is not None and score != best_score:
            break
        best_score = score
        try:
            text = _fetch_text(candidate)
        except Exception as exc:
            errors.append(f"{getattr(candidate, 'language_code', 'unknown')}: {exc}")
            continue

        if not text.strip():
            errors.append(f"{getattr(candidate, 'language_code', 'unknown')}: empty transcript")
            continue

        source = "auto_caption" if bool(getattr(candidate, "is_generated", False)) else "manual_caption"
        result = TranscriptResult(
            status="success",
            language=getattr(candidate, "language_code", None),
            source=source,
            text=text,
        )
        if best_result is None or len(result.text) > len(best_result.text):
            best_result = result

    if best_result is not None:
        return best_result

    return _return_with_yt_dlp_fallback(
        video_id,
        transcript_config,
        preferred_languages,
        allow_auto_captions,
        TranscriptResult(
            status="failed",
            error_message="; ".join(errors) if errors else "Matching transcripts could not be fetched",
        ),
    )
