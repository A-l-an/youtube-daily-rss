from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


SEQUENCE_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\Z")


def validate_sequence(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError(f"Backfill request sequence file is missing: {path}") from exc

    if not raw:
        raise ValueError("Backfill request sequence file is empty")
    if b"\r" in raw:
        raise ValueError("Backfill request sequence must use LF only; CR/CRLF is not allowed")

    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if b"\n" in raw:
        raise ValueError("Backfill request sequence must contain exactly one line")

    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("Backfill request sequence must contain ASCII digits only") from exc

    if not SEQUENCE_PATTERN.fullmatch(value):
        raise ValueError(
            "Backfill request sequence must be a canonical non-negative decimal integer"
        )
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate one fail-closed Git-push retry sequence"
    )
    parser.add_argument("sequence_path", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        validated = validate_sequence(args.sequence_path)
    except (OSError, ValueError) as exc:
        print(f"Invalid backfill request sequence: {exc}", file=sys.stderr)
        return 2
    print(validated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
