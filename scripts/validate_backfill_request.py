from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys
from typing import Any, Dict, Optional

from main import load_config, parse_ended_report_date


def read_request_date(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError(f"Backfill request file is missing: {path}") from exc

    if not raw:
        raise ValueError("Backfill request file is empty")
    if b"\r" in raw:
        raise ValueError("Backfill request must use LF only; CR/CRLF is not allowed")

    # A conventional single trailing LF is allowed, but embedded or repeated
    # newlines are rejected so only one request can enter a workflow run.
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if b"\n" in raw:
        raise ValueError("Backfill request must contain exactly one line")

    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("Backfill request must contain ASCII YYYY-MM-DD only") from exc

    # parse_ended_report_date enforces the exact ten-character format, calendar
    # validity, and that the date has ended in the configured local timezone.
    return value


def validate_request(
    path: Path,
    config: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> str:
    value = read_request_date(path)
    report_date = parse_ended_report_date(value, config or load_config(), now=now)
    return report_date.isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate one fail-closed Git-push report-date request"
    )
    parser.add_argument("request_path", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        validated = validate_request(args.request_path)
    except (OSError, ValueError) as exc:
        print(f"Invalid backfill request: {exc}", file=sys.stderr)
        return 2
    print(validated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
