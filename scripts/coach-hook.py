#!/usr/bin/env python3
"""
Stop-hook companion to watch.py.

Reads Claude Code hook JSON from stdin, analyzes the session's JSONL, and
prints an advisory line (to stderr) if thresholds are hit. Never blocks.

Stop hook payload (relevant fields):
    {
      "session_id": "...",
      "transcript_path": "/abs/path/to/session.jsonl",
      "cwd": "...",
      ...
    }

Exit codes:
    0 — always (advisory only, never blocks Claude)

Thresholds (tunable via env vars):
    COACH_TURN_WARN          default 20
    COACH_CACHE_WARN_PCT     default 40
    COACH_BUST_DROP          default 30000
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


TURN_WARN = env_int("COACH_TURN_WARN", 20)
CACHE_WARN_PCT = env_float("COACH_CACHE_WARN_PCT", 40.0)
BUST_DROP = env_int("COACH_BUST_DROP", 30_000)


def analyze(transcript_path: Path) -> dict:
    turns = 0
    total_in = 0
    total_cr = 0
    total_cw = 0
    busts = 0
    prev_cr = None
    if not transcript_path.exists():
        return {"turns": 0, "cache_pct": 0.0, "busts": 0}
    with transcript_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = rec.get("message") or {}
            usage = msg.get("usage")
            if not usage:
                continue
            turns += 1
            inp = usage.get("input_tokens", 0) or 0
            cr = usage.get("cache_read_input_tokens", 0) or 0
            cw = usage.get("cache_creation_input_tokens", 0) or 0
            total_in += inp
            total_cr += cr
            total_cw += cw
            if prev_cr is not None:
                drop = prev_cr - cr
                if prev_cr > BUST_DROP and drop > BUST_DROP and cw > BUST_DROP:
                    busts += 1
            prev_cr = cr
    denom = total_in + total_cr + total_cw
    cache_pct = (total_cr / denom * 100) if denom else 0.0
    return {"turns": turns, "cache_pct": cache_pct, "busts": busts}


def build_advisory(stats: dict) -> str | None:
    tips = []
    if stats["turns"] >= TURN_WARN:
        tips.append(
            f"{stats['turns']} turns this session — run `/compact` to summarize "
            f"in place, or `/clear` and paste a one-paragraph summary."
        )
    if stats["turns"] >= 5 and stats["cache_pct"] < CACHE_WARN_PCT:
        tips.append(
            f"cache hit was only {stats['cache_pct']:.1f}% — likely cause: "
            f"switching projects mid-session or edits that bust prompt cache."
        )
    if stats["busts"]:
        tips.append(
            f"{stats['busts']} cache-bust event(s) detected — each re-pays the "
            f"full input cost."
        )
    if not tips:
        return None
    return "[usage-coach] " + " | ".join(tips)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    transcript = payload.get("transcript_path")
    if not transcript:
        return 0
    stats = analyze(Path(transcript))
    advisory = build_advisory(stats)
    if advisory:
        print(advisory, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
