from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import yaml

from fetch_youtube import Video, fetch_latest_video


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yml"


def load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def check_channels(channels: Iterable[Dict[str, Any]]) -> Tuple[List[Video], List[Tuple[str, str]]]:
    successes: List[Video] = []
    failures: List[Tuple[str, str]] = []

    for channel in channels:
        channel_id = str(channel.get("channel_id") or "missing-channel-id")
        label = f"{channel.get('name') or channel_id} ({channel_id})"
        try:
            video = fetch_latest_video(channel)
        except Exception as exc:
            failures.append((label, str(exc)))
            logging.warning("[preflight] %s unavailable: %s", label, exc)
            continue

        successes.append(video)
        logging.info("[preflight] %s OK latest=%s title=%s", label, video.video_id, video.title)

    return successes, failures


def run(config_path: Path, min_success: int) -> int:
    config = load_config(config_path)
    channels = config.get("channels") or []
    if not channels:
        logging.error("YouTube RSS preflight failed: no channels configured in %s", config_path)
        return 1

    successes, failures = check_channels(channels)
    if len(successes) < min_success:
        logging.error(
            "YouTube RSS preflight failed: %d/%d channel feed(s) reachable; need at least %d",
            len(successes),
            len(channels),
            min_success,
        )
        for label, message in failures:
            logging.error("[preflight] %s error: %s", label, message)
        return 1

    if failures:
        logging.warning(
            "YouTube RSS preflight passed with %d/%d channel feed(s) reachable",
            len(successes),
            len(channels),
        )
    else:
        logging.info("YouTube RSS preflight passed: all %d channel feed(s) reachable", len(successes))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate configured YouTube Atom feeds")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="Path to config.yml")
    parser.add_argument("--min-success", type=int, default=1, help="Minimum reachable channel feeds required")
    parser.add_argument("--log-level", default="INFO", help="Python logging level")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return run(args.config, args.min_success)


if __name__ == "__main__":
    raise SystemExit(main())
