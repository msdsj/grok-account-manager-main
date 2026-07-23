"""Small shared helpers for the FastAPI backend."""

from __future__ import annotations

from pathlib import Path
import time


def now_ms() -> int:
    return int(time.time() * 1000)


def json_default(value):
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def safe_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))

