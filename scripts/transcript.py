from __future__ import annotations

from dataclasses import dataclass, asdict
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


def get_transcript(video_id: str, transcript_config: Dict[str, Any]) -> TranscriptResult:
    preferred_languages = transcript_config.get("preferred_languages") or ["zh-Hans", "zh", "zh-CN", "en"]
    allow_auto_captions = bool(transcript_config.get("allow_auto_captions", True))

    try:
        transcript_list = list(_list_transcripts(video_id))
    except Exception as exc:
        status = "unavailable" if _is_unavailable_exception(exc) else "failed"
        return TranscriptResult(status=status, error_message=str(exc))

    ranked: List[Tuple[Tuple[int, int], Any]] = []
    for transcript in transcript_list:
        score = _candidate_score(transcript, preferred_languages, allow_auto_captions)
        if score is not None:
            ranked.append((score, transcript))

    if not ranked:
        available = sorted({getattr(t, "language_code", "unknown") for t in transcript_list})
        return TranscriptResult(
            status="unavailable",
            error_message=f"No matching transcript languages. Available: {', '.join(available) or 'none'}",
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

    return TranscriptResult(
        status="failed",
        error_message="; ".join(errors) if errors else "Matching transcripts could not be fetched",
    )
