"""Finite lifetimes and source-change detection shared by all running loops."""
from __future__ import annotations

import datetime as dt
import hashlib
import math
from pathlib import Path
import re
import time


def duration(value: str) -> float:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?)([smh]?)", value)
    if not match:
        raise ValueError("--for must be a positive finite duration, e.g. 30s, 10m, 4h")
    seconds = float(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[match[2]]
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("--for must be positive and finite")
    # A duration must also have a representable signed expiry.
    try:
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)
    except OverflowError as exc:
        raise ValueError("--for exceeds the supported calendar range") from exc
    return seconds


WIND_DOWN = 1200.0


def wind_down(window_seconds: float) -> float:
    """How long before the signed deadline a shift stops working: twenty minutes,
    or the last third of a window shorter than an hour. Fixed by the signed
    window alone, so it can be shown before signing."""
    if not math.isfinite(window_seconds) or window_seconds <= 0:
        raise ValueError("a positive finite window is required")
    return min(WIND_DOWN, window_seconds / 3)


CLOSING = 10.0


def closing(wind_down_seconds: float) -> float:
    """How long before the signed deadline the host stops everything it started,
    so that stopping and the report are done by the deadline: ten seconds, or
    half of a wind-down shorter than twenty seconds."""
    if not math.isfinite(wind_down_seconds) or wind_down_seconds <= 0:
        raise ValueError("a positive finite wind-down is required")
    return min(CLOSING, wind_down_seconds / 2)


class Budget:
    def __init__(self, seconds, *, until=None, clock=time.monotonic,
                 now=lambda: dt.datetime.now(dt.timezone.utc)):
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("a positive finite --for duration is required")
        if until is not None and (until.tzinfo is None or until.utcoffset() is None):
            raise ValueError("expiry must have a timezone")
        self.clock, self.now, self.until = clock, now, until
        limit = min(seconds, (until - now()).total_seconds()) if until else seconds
        self.end = clock() + max(0, limit)

    def remaining(self):
        left = self.end - self.clock()
        if self.until is not None:
            left = min(left, (self.until - self.now()).total_seconds())
        return max(0.0, left)

    def guard(self):
        return "duration elapsed" if self.remaining() <= 0 else ""


class Sources:
    """Pin all code and packaged assets, including additions and removals."""
    def __init__(self, root=None):
        self.root = Path(root or Path(__file__).parent).resolve()
        self.original = self.snapshot()

    def snapshot(self):
        result = {}
        for path in sorted(self.root.rglob("*")):
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                raise ValueError("source manifest refuses symlinks")
            if path.is_file():
                result[path.relative_to(self.root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        if not result:
            raise ValueError("source manifest is empty")
        return result

    def guard(self):
        try:
            return "source changed" if self.snapshot() != self.original else ""
        except (OSError, ValueError):
            return "source unavailable or changed"
