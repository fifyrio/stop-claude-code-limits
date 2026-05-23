#!/usr/bin/env python3
"""
Live Claude Code usage coach.

Tails the active session's JSONL log and prints a live dashboard:
turn count, rolling cache-hit %, 5-hour-window burn, and cache-bust alerts.

Stdlib-only; no watchdog dep. Polls file mtime every --interval seconds.

Usage:
    python3 watch.py                       # auto-detect current project (cwd)
    python3 watch.py --project /abs/path   # explicit project root
    python3 watch.py --session <uuid>      # specific session JSONL
    python3 watch.py --interval 3          # poll every 3 seconds
    python3 watch.py --quiet               # only print alerts, no dashboard
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Same prices as usage-report.py — keep in sync.
PRICES = {
    "claude-opus-4":     (15.0, 75.0, 18.75, 1.50),
    "claude-opus-4-1":   (15.0, 75.0, 18.75, 1.50),
    "claude-opus-4-5":   (15.0, 75.0, 18.75, 1.50),
    "claude-opus-4-6":   (15.0, 75.0, 18.75, 1.50),
    "claude-opus-4-7":   (15.0, 75.0, 18.75, 1.50),
    "claude-sonnet-4":   (3.0, 15.0, 3.75, 0.30),
    "claude-sonnet-4-5": (3.0, 15.0, 3.75, 0.30),
    "claude-sonnet-4-6": (3.0, 15.0, 3.75, 0.30),
    "claude-haiku-4-5":  (1.0, 5.0, 1.25, 0.10),
}

# Thresholds for the live coach. Tunable via CLI.
DEFAULT_TURN_WARN = 20
DEFAULT_CACHE_WARN_PCT = 40.0
DEFAULT_CACHE_BUST_DROP = 30_000
DEFAULT_INTERVAL_SECS = 5


def price_for(model: str) -> tuple[float, float, float, float]:
    if not model:
        return (0.0, 0.0, 0.0, 0.0)
    key = model.lower()
    for known in PRICES:
        if key.startswith(known):
            return PRICES[known]
    return (0.0, 0.0, 0.0, 0.0)


def cwd_to_project_dir(projects_dir: Path, cwd: str) -> Path:
    # Claude Code encodes cwd by replacing "/" with "-".
    encoded = cwd.replace("/", "-")
    return projects_dir / encoded


def find_session_file(project_dir: Path, session: str | None) -> Path | None:
    if not project_dir.exists():
        return None
    if session:
        candidate = project_dir / f"{session}.jsonl"
        return candidate if candidate.exists() else None
    files = sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def parse_usage_record(rec: dict) -> dict | None:
    msg = rec.get("message") or {}
    usage = msg.get("usage")
    if not usage:
        return None
    ts = rec.get("timestamp")
    when = None
    if ts:
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            pass
    model = msg.get("model") or ""
    content = msg.get("content") or []
    tools = [
        c.get("name") or "(unknown)"
        for c in content
        if isinstance(c, dict) and c.get("type") == "tool_use"
    ]
    inp = usage.get("input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    cr = usage.get("cache_read_input_tokens", 0) or 0
    cw = usage.get("cache_creation_input_tokens", 0) or 0
    pin, pout, pcw, pcr = price_for(model)
    cost = (inp * pin + out * pout + cw * pcw + cr * pcr) / 1_000_000
    return {
        "when": when,
        "model": model,
        "input": inp,
        "output": out,
        "cache_read": cr,
        "cache_write": cw,
        "cost": cost,
        "tools": tools,
    }


class Coach:
    def __init__(self, args):
        self.args = args
        self.turns = 0
        self.total_in = 0
        self.total_cache_read = 0
        self.total_cache_write = 0
        self.total_out = 0
        self.total_cost = 0.0
        # 5-hour rolling window: keep (when, cost) entries.
        self.window: deque[tuple[datetime, float]] = deque()
        self.last_turn = None
        self.alerts_fired: set[str] = set()

    def _trim_window(self, now: datetime) -> None:
        cutoff = now - timedelta(hours=5)
        while self.window and self.window[0][0] < cutoff:
            self.window.popleft()

    def _window_cost(self) -> float:
        return sum(c for _, c in self.window)

    def _cache_pct(self) -> float:
        total = self.total_in + self.total_cache_read + self.total_cache_write
        if not total:
            return 0.0
        return self.total_cache_read / total * 100

    def ingest(self, turn: dict) -> list[str]:
        alerts: list[str] = []
        self.turns += 1
        self.total_in += turn["input"]
        self.total_out += turn["output"]
        self.total_cache_read += turn["cache_read"]
        self.total_cache_write += turn["cache_write"]
        self.total_cost += turn["cost"]
        when = turn["when"] or datetime.now(timezone.utc)
        self.window.append((when, turn["cost"]))
        self._trim_window(when)

        # Cache-bust detection
        if self.last_turn is not None:
            drop = self.last_turn["cache_read"] - turn["cache_read"]
            if (
                self.last_turn["cache_read"] > self.args.bust_drop
                and drop > self.args.bust_drop
                and turn["cache_write"] > self.args.bust_drop
            ):
                tools = ",".join(turn["tools"]) or "(none)"
                alerts.append(
                    f"CACHE-BUST: cache_read dropped by {fmt_tokens(drop)} and "
                    f"{fmt_tokens(turn['cache_write'])} new cache_write. "
                    f"This turn's tools: [{tools}]."
                )

        # Turn-count warning (one-shot)
        if (
            self.turns >= self.args.turn_warn
            and "turn_warn" not in self.alerts_fired
        ):
            self.alerts_fired.add("turn_warn")
            alerts.append(
                f"TURN COUNT: {self.turns} turns. Run `/compact` to summarize in "
                f"place, or `/clear` and start fresh with a one-paragraph summary."
            )

        # Cache % warning (only after we have enough signal)
        if (
            self.turns >= 5
            and self._cache_pct() < self.args.cache_warn_pct
            and "cache_warn" not in self.alerts_fired
        ):
            self.alerts_fired.add("cache_warn")
            alerts.append(
                f"LOW CACHE HIT: {self._cache_pct():.1f}% (target >=60%). "
                f"Likely cause: switching projects mid-session, editing files in "
                f"ways that bust cache, or frequent /clear."
            )

        self.last_turn = turn
        return alerts

    def render_dashboard(self) -> str:
        pct = self._cache_pct()
        window_cost = self._window_cost()
        return (
            f"turns={self.turns:>3}  "
            f"cache_hit={pct:5.1f}%  "
            f"in={fmt_tokens(self.total_in):>7}  "
            f"out={fmt_tokens(self.total_out):>7}  "
            f"cache_r={fmt_tokens(self.total_cache_read):>7}  "
            f"5h_burn=${window_cost:6.2f}  "
            f"total=${self.total_cost:6.2f}"
        )


def tail_jsonl(path: Path, interval: float, start_pos: int = 0):
    """Generator: yields each new line as it appears. Survives file truncation."""
    pos = start_pos
    while True:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            time.sleep(interval)
            continue
        if size < pos:
            # File rotated/truncated.
            pos = 0
        if size > pos:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(pos)
                for line in f:
                    line = line.rstrip("\n")
                    if line:
                        yield line
                pos = f.tell()
        time.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=str, default=os.getcwd(),
                    help="Project cwd to watch (default: $PWD)")
    ap.add_argument("--session", type=str, default=None,
                    help="Specific session UUID; otherwise newest JSONL in project dir")
    ap.add_argument("--projects-dir", type=str,
                    default=str(Path.home() / ".claude" / "projects"))
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECS)
    ap.add_argument("--turn-warn", type=int, default=DEFAULT_TURN_WARN)
    ap.add_argument("--cache-warn-pct", type=float, default=DEFAULT_CACHE_WARN_PCT)
    ap.add_argument("--bust-drop", type=int, default=DEFAULT_CACHE_BUST_DROP)
    ap.add_argument("--quiet", action="store_true",
                    help="Suppress dashboard line; only print alerts")
    ap.add_argument("--from-start", action="store_true",
                    help="Replay from start of file (default: tail new lines only)")
    args = ap.parse_args()

    projects_dir = Path(args.projects_dir)
    project_dir = cwd_to_project_dir(projects_dir, args.project)
    session_file = find_session_file(project_dir, args.session)
    if session_file is None:
        print(f"No session JSONL found in {project_dir}", file=sys.stderr)
        print("Start Claude Code in that project first, then re-run this watcher.",
              file=sys.stderr)
        return 1

    print(f"Watching {session_file}", file=sys.stderr)
    coach = Coach(args)

    # Determine where to start tailing. Default: skip existing content (tail only
    # new lines). --from-start replays history first, then tails.
    if args.from_start:
        start_pos = 0
        with session_file.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                turn = parse_usage_record(rec)
                if turn:
                    for a in coach.ingest(turn):
                        print(f"[ALERT] {a}", flush=True)
            start_pos = f.tell()
        if not args.quiet:
            print(coach.render_dashboard(), flush=True)
    else:
        try:
            start_pos = session_file.stat().st_size
        except OSError:
            start_pos = 0

    try:
        for line in tail_jsonl(session_file, args.interval, start_pos=start_pos):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            turn = parse_usage_record(rec)
            if not turn:
                continue
            alerts = coach.ingest(turn)
            for a in alerts:
                print(f"[ALERT] {a}", flush=True)
            if not args.quiet:
                print(coach.render_dashboard(), flush=True)
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
