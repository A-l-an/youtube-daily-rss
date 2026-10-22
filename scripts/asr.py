from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from ai_client import make_openai_compatible_client, resolve_ai_credentials


XIEMAC_TRANSCRIBE = Path.home() / ".local/bin/xiemac-transcribe"
XIEMAC_PROCESS_TIMEOUT = 210
XIEMAC_SSH = "ssh"
XIEMAC_IDLE_PROBE = """import json,subprocess,urllib.request,urllib.error
health={}
for port in (18766,18768):
    try:
        response=urllib.request.urlopen('http://127.0.0.1:%s/health'%port,timeout=5)
    except urllib.error.HTTPError as error:
        response=error
    with response:
        health[str(port)]={'code':response.code,'status':json.load(response).get('status')}
result=subprocess.run(['lsof','-nP','-iTCP:18766','-iTCP:18768','-iTCP:18769','-iTCP:18770',
                       '-sTCP:ESTABLISHED','-Fpcn'],capture_output=True,text=True,timeout=3)
if result.returncode not in (0,1) or result.stderr.strip():
    raise SystemExit(2)
print(json.dumps({'health':health,'connections':len(result.stdout.strip().splitlines())}))
"""


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
    try:
        import ytdlp_opts

        ytdlp_opts.merge_into(options, audio=True)
    except Exception as exc:  # never let access-hardening break the legacy path
        import logging

        logging.warning("ytdlp_opts.merge_into (audio) skipped: %s", exc)
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


def _check_xiemac_idle() -> None:
    host = os.environ.get("XIEMAC_ASR_HOST", "xiemac")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", host) is None:
        raise RuntimeError("xiemac ASR: ssh_unavailable")
    try:
        probe = subprocess.run(
            [str(XIEMAC_SSH), "-T", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes",
             "-oConnectTimeout=5", host, "python3 -c " + shlex.quote(XIEMAC_IDLE_PROBE)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=20, check=True,
        )
        result = json.loads(probe.stdout)
        health, connections = result["health"], result["connections"]
        live = health["18766"]
        refined = health["18768"]
        known = (live, refined) in (
            ({"code": 200, "status": "process_running"}, {"code": 200, "status": "standby"}),
            ({"code": 200, "status": "process_running"}, {"code": 503, "status": "live_priority"}),
            ({"code": 200, "status": "standby"}, {"code": 200, "status": "process_running"}),
            ({"code": 200, "status": "standby"}, {"code": 200, "status": "standby"}),
        )
        if not known or type(connections) is not int or connections < 0:
            raise ValueError("Uncertain shared ASR status")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError):
        raise RuntimeError("xiemac ASR: remote_unavailable") from None
    if connections:
        raise RuntimeError("xiemac ASR: remote_busy")


def _transcribe_xiemac_file(model: str, audio_path: Path, language: Optional[str]) -> str:
    # This read-only snapshot avoids observed busy work; it is not an atomic reservation.
    _check_xiemac_idle()
    try:
        with subprocess.Popen(
            [str(XIEMAC_TRANSCRIBE), str(audio_path), "--language", language or "auto",
             "--model", model, "--timeout", "180"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
        ) as process:
            try:
                stdout, _ = process.communicate(timeout=XIEMAC_PROCESS_TIMEOUT)
            except subprocess.TimeoutExpired:
                # The CLI handles SIGTERM and cleans its own SSH/ffmpeg process groups.
                process.terminate()
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise RuntimeError("xiemac ASR: timeout") from None
            returncode = process.returncode
    except OSError:
        raise RuntimeError("xiemac ASR: cli_unavailable") from None

    try:
        result = json.loads(stdout)
    except ValueError:
        raise RuntimeError("xiemac ASR: invalid_response") from None
    if not isinstance(result, dict) or result.get("provider") != "remote-whisper.cpp":
        raise RuntimeError("xiemac ASR: invalid_response")
    if returncode != 0 or "error" in result:
        error = result.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        if code not in {"remote_busy", "timeout", "remote_unavailable", "ssh_unavailable",
                        "invalid_response", "audio_too_large", "response_too_large", "cancelled"}:
            code = "client_failed"
        raise RuntimeError(f"xiemac ASR: {code}")
    text = result.get("text")
    if result.get("model") != model or not isinstance(text, str) or not text.strip():
        raise RuntimeError("xiemac ASR: invalid_response")
    return text.strip()


def _transcribe_chunks(
    client: Any,
    model: str,
    chunk_paths: List[Path],
    language: Optional[str],
    *,
    provider: str = "hkust_gz",
) -> str:
    parts: List[str] = []
    for chunk_path in chunk_paths:
        if provider == "xiemac":
            text = _transcribe_xiemac_file(model, chunk_path, language)
        else:
            text = _transcribe_file(client, model, chunk_path, language).strip()
        if text:
            parts.append(text)
        time.sleep(1)
    return "\n\n".join(parts)


def transcribe_video(video_url: str, asr_config: Dict[str, Any]) -> ASRResult:
    if not asr_config.get("enabled", False):
        return ASRResult(status="skipped", error_message="ASR is disabled in config")

    provider = asr_config.get("provider", "hkust_gz")
    model = os.getenv("ASR_MODEL") or asr_config.get("model", "large-v3-turbo" if provider == "xiemac" else "whisper-1")
    if provider == "xiemac":
        if model not in {"large-v3", "large-v3-turbo"}:
            return ASRResult(status="failed", model=model, error_message="xiemac ASR: unsupported_model; check ASR_MODEL")
    else:
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
        client = None if provider == "xiemac" else make_openai_compatible_client(credentials)
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
                min(int(asr_config.get("chunk_seconds", 15)), 15) if provider == "xiemac"
                else int(asr_config.get("chunk_seconds", 300)),
            )

            language_candidates = asr_config.get("language_candidates")
            if not language_candidates:
                language_candidates = [asr_config.get("language", "zh")]

            errors: List[str] = []
            text = ""
            for language in language_candidates:
                try:
                    text = _transcribe_chunks(client, model, chunk_paths, language, provider=provider)
                    if text:
                        break
                except Exception as exc:
                    errors.append(f"{language or 'auto'}: {exc}")
                    if provider == "xiemac":
                        break

        if not text:
            message = "; ".join(errors) if errors else "ASR returned empty text"
            return ASRResult(status="failed", model=model, error_message=message)
        return ASRResult(status="success", model=model, text=text)
    except Exception as exc:
        return ASRResult(status="failed", model=model, error_message=str(exc))
