from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from ai_client import make_openai_compatible_client, resolve_ai_credentials


@dataclass
class ASRResult:
    status: str
    model: Optional[str] = None
    text: str = ""
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _audio_candidates(output_dir: Path) -> List[Path]:
    ignored_suffixes = {".part", ".ytdl", ".json"}
    return [
        path
        for path in sorted(output_dir.glob("audio.*"))
        if path.is_file() and path.suffix.lower() not in ignored_suffixes
    ]


def _download_audio(
    video_url: str,
    output_dir: Path,
    attempts: int = 3,
    sleep_seconds: float = 5,
) -> Path:
    import yt_dlp

    outtmpl = str(output_dir / "audio.%(ext)s")
    options = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        "continuedl": True,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "socket_timeout": 30,
        "extractor_args": {"youtube": {"player_client": ["android", "mweb", "web"]}},
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "64",
            }
        ],
    }
    last_error: Optional[Exception] = None
    max_attempts = max(1, attempts)
    for attempt in range(1, max_attempts + 1):
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([video_url])
        except Exception as exc:
            last_error = exc
            candidates = _audio_candidates(output_dir)
            if candidates:
                return candidates[0]
            if attempt < max_attempts:
                time.sleep(sleep_seconds * attempt)
            continue

        candidates = _audio_candidates(output_dir)
        if candidates:
            return candidates[0]
        last_error = RuntimeError("yt-dlp did not produce an audio file")
        if attempt < max_attempts:
            time.sleep(sleep_seconds * attempt)

    message = str(last_error) if last_error else "yt-dlp did not produce an audio file"
    raise RuntimeError(f"yt-dlp audio download failed after {max_attempts} attempt(s): {message}")


def _chunk_audio(audio_path: Path, output_dir: Path, chunk_seconds: int) -> List[Path]:
    from pydub import AudioSegment

    audio = AudioSegment.from_file(audio_path)
    chunk_ms = chunk_seconds * 1000
    if len(audio) <= chunk_ms:
        return [audio_path]

    chunks: List[Path] = []
    for idx, start in enumerate(range(0, len(audio), chunk_ms), start=1):
        chunk = audio[start : start + chunk_ms]
        chunk_path = output_dir / f"chunk-{idx:03d}.mp3"
        chunk.export(chunk_path, format="mp3", bitrate="64k")
        chunks.append(chunk_path)
    return chunks


def _extract_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return (result.get("text") or result.get("transcript") or "").strip()
    text = getattr(result, "text", None) or getattr(result, "transcript", None)
    if text:
        return str(text).strip()
    return str(result).strip()


def _transcribe_file(
    client: Any,
    model: str,
    audio_path: Path,
    language: Optional[str],
    attempts: int = 3,
) -> str:
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            with audio_path.open("rb") as fh:
                kwargs: Dict[str, Any] = {
                    "model": model,
                    "file": fh,
                    "response_format": "verbose_json",
                }
                if language:
                    kwargs["language"] = language
                result = client.audio.transcriptions.create(**kwargs)
            return _extract_text(result)
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(2 * attempt)
    raise RuntimeError(str(last_error))


def _transcribe_chunks(
    client: Any,
    model: str,
    chunk_paths: List[Path],
    language: Optional[str],
) -> str:
    parts: List[str] = []
    for chunk_path in chunk_paths:
        text = _transcribe_file(client, model, chunk_path, language).strip()
        if text:
            parts.append(text)
        time.sleep(1)
    return "\n\n".join(parts)


def transcribe_video(video_url: str, asr_config: Dict[str, Any]) -> ASRResult:
    if not asr_config.get("enabled", False):
        return ASRResult(status="skipped", error_message="ASR is disabled in config")

    model = os.getenv("ASR_MODEL") or asr_config.get("model", "whisper-1")
    try:
        credentials = resolve_ai_credentials(asr_config, default_provider="hkust_gz")
    except Exception as exc:
        return ASRResult(status="skipped", model=model, error_message=str(exc))

    if not asr_config.get("allow_youtube_audio_download", True):
        return ASRResult(
            status="skipped",
            model=model,
            error_message="YouTube audio download is disabled by config",
        )

    if not shutil.which("ffmpeg"):
        return ASRResult(status="failed", model=model, error_message="ffmpeg is required for ASR")

    try:
        client = make_openai_compatible_client(credentials)
        with tempfile.TemporaryDirectory(prefix="youtube-rss-asr-") as tmp:
            tmpdir = Path(tmp)
            audio_path = _download_audio(
                video_url,
                tmpdir,
                attempts=int(asr_config.get("download_attempts", 3)),
                sleep_seconds=float(asr_config.get("download_sleep_seconds", 5)),
            )
            chunk_paths = _chunk_audio(
                audio_path,
                tmpdir,
                int(asr_config.get("chunk_seconds", 300)),
            )

            language_candidates = asr_config.get("language_candidates")
            if not language_candidates:
                language_candidates = [asr_config.get("language", "zh")]

            errors: List[str] = []
            text = ""
            for language in language_candidates:
                try:
                    text = _transcribe_chunks(client, model, chunk_paths, language)
                    if text:
                        break
                except Exception as exc:
                    errors.append(f"{language or 'auto'}: {exc}")

        if not text:
            message = "; ".join(errors) if errors else "ASR returned empty text"
            return ASRResult(status="failed", model=model, error_message=message)
        return ASRResult(status="success", model=model, text=text)
    except Exception as exc:
        return ASRResult(status="failed", model=model, error_message=str(exc))
