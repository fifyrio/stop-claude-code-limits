#!/usr/bin/env python3
"""
Local Claude Code token-usage report.

Reads ~/.claude/projects/*/*.jsonl (written by Claude Code on every turn) and
prints a breakdown by model, project, and day. No network calls — everything
stays on disk.

Usage:
    python3 usage-report.py                  # last 7 days, all projects
    python3 usage-report.py --days 1         # today-ish
    python3 usage-report.py --days 30
    python3 usage-report.py --project /path  # scope to one cwd
    python3 usage-report.py --json           # machine-readable output

Prices below are the public API list prices as of writing; they are estimates
for relative comparison, not a bill. Claude Code subscription plans don't bill
per-token, so treat the $ figure as "what this would cost on the API."
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Per-million-token list prices (USD). Update as Anthropic publishes new ones.
PRICES = {
    # (input, output, cache_write_5m, cache_read)
    "claude-opus-4":          (15.0, 75.0, 18.75, 1.50),
    "claude-opus-4-1":        (15.0, 75.0, 18.75, 1.50),
    "claude-opus-4-5":        (15.0, 75.0, 18.75, 1.50),
    "claude-opus-4-6":        (15.0, 75.0, 18.75, 1.50),
    "claude-opus-4-7":        (15.0, 75.0, 18.75, 1.50),
    "claude-sonnet-4":        (3.0, 15.0, 3.75, 0.30),
    "claude-sonnet-4-5":      (3.0, 15.0, 3.75, 0.30),
    "claude-sonnet-4-6":      (3.0, 15.0, 3.75, 0.30),
    "claude-haiku-4-5":       (1.0, 5.0, 1.25, 0.10),
    "claude-3-5-sonnet":      (3.0, 15.0, 3.75, 0.30),
    "claude-3-5-haiku":       (0.80, 4.0, 1.0, 0.08),
}


def price_for(model: str) -> tuple[float, float, float, float]:
    if not model:
        return (0.0, 0.0, 0.0, 0.0)
    key = model.lower()
    # strip date suffix like "-20251001"
    for known in PRICES:
        if key.startswith(known):
            return PRICES[known]
    return (0.0, 0.0, 0.0, 0.0)


def iter_jsonl(paths):
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue


def _zero_row():
    return {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
        "turns": 0, "cost": 0.0,
    }


def _tool_names_from_content(content) -> list[str]:
    if not isinstance(content, list):
        return []
    names = []
    for c in content:
        if isinstance(c, dict) and c.get("type") == "tool_use":
            names.append(c.get("name") or "(unknown)")
    return names


def collect(projects_dir: Path, cutoff: datetime, project_filter: str | None):
    totals = _zero_row()
    by_model = defaultdict(_zero_row)
    by_project = defaultdict(_zero_row)
    by_day = defaultdict(_zero_row)
    by_tool = defaultdict(_zero_row)
    cache_busts: list[dict] = []

    files = list(projects_dir.glob("*/*.jsonl"))
    # Process per-file so cache-bust detection sees the session's turn order.
    for path in files:
        prev = None
        for rec in iter_jsonl([path]):
            msg = rec.get("message") or {}
            usage = msg.get("usage")
            if not usage:
                continue
            ts = rec.get("timestamp")
            if ts:
                try:
                    when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    when = None
            else:
                when = None
            if when and when < cutoff:
                continue
            cwd = rec.get("cwd") or "(unknown)"
            if project_filter and project_filter not in cwd:
                continue
            model = msg.get("model") or "(unknown)"
            day = when.date().isoformat() if when else "(no date)"

            inp = usage.get("input_tokens", 0) or 0
            out = usage.get("output_tokens", 0) or 0
            cr = usage.get("cache_read_input_tokens", 0) or 0
            cw = usage.get("cache_creation_input_tokens", 0) or 0

            pin, pout, pcw, pcr = price_for(model)
            cost = (inp * pin + out * pout + cw * pcw + cr * pcr) / 1_000_000

            for bucket in (totals, by_model[model], by_project[cwd], by_day[day]):
                bucket["input"] += inp
                bucket["output"] += out
                bucket["cache_read"] += cr
                bucket["cache_write"] += cw
                bucket["turns"] += 1
                bucket["cost"] += cost

            # Tool attribution: pro-rate this turn's output + cache-write cost
            # across tool_use calls in the assistant message. Cache-read is
            # dominated by re-read of prior context, so we don't blame tools
            # for it — only what *this* turn produced.
            tool_names = _tool_names_from_content(msg.get("content"))
            if tool_names:
                turn_attrib_cost = (out * pout + cw * pcw) / 1_000_000
                share = turn_attrib_cost / len(tool_names)
                share_out = out // len(tool_names)
                share_cw = cw // len(tool_names)
                for name in tool_names:
                    b = by_tool[name]
                    b["turns"] += 1
                    b["output"] += share_out
                    b["cache_write"] += share_cw
                    b["cost"] += share

            # Cache-bust detection: previous turn in same session had a big
            # cache_read, this turn dropped it AND created a new big cache.
            # Heuristic threshold: drop > 30k AND new write > 30k.
            if prev is not None:
                prev_cr = prev["cache_read"]
                drop = prev_cr - cr
                if prev_cr > 30_000 and drop > 30_000 and cw > 30_000:
                    cache_busts.append({
                        "when": when.isoformat() if when else None,
                        "cwd": cwd,
                        "model": model,
                        "prev_cache_read": prev_cr,
                        "this_cache_read": cr,
                        "this_cache_write": cw,
                        "drop": drop,
                        "tools": tool_names,
                        "prev_tools": prev["tools"],
                    })
            prev = {
                "cache_read": cr,
                "cache_write": cw,
                "tools": tool_names,
            }

    return (
        totals,
        dict(by_model),
        dict(by_project),
        dict(by_day),
        dict(by_tool),
        cache_busts,
    )


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def print_row(label: str, row: dict, width: int = 40):
    total_in = row["input"] + row["cache_read"] + row["cache_write"]
    cache_pct = (row["cache_read"] / total_in * 100) if total_in else 0.0
    print(
        f"  {label[:width]:<{width}} "
        f"turns={row['turns']:>5}  "
        f"in={fmt_tokens(row['input']):>7}  "
        f"out={fmt_tokens(row['output']):>7}  "
        f"cache_r={fmt_tokens(row['cache_read']):>7} ({cache_pct:4.1f}%)  "
        f"cache_w={fmt_tokens(row['cache_write']):>7}  "
        f"~${row['cost']:>7.2f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--project", type=str, default=None, help="substring match on cwd")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--projects-dir", type=str, default=str(Path.home() / ".claude" / "projects"))
    args = ap.parse_args()

    projects_dir = Path(args.projects_dir)
    if not projects_dir.exists():
        print(f"No Claude Code logs found at {projects_dir}", file=sys.stderr)
        return 1

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    totals, by_model, by_project, by_day, by_tool, cache_busts = collect(
        projects_dir, cutoff, args.project
    )

    if args.json:
        print(json.dumps({
            "window_days": args.days,
            "totals": totals,
            "by_model": by_model,
            "by_project": by_project,
            "by_day": by_day,
            "by_tool": by_tool,
            "cache_busts": cache_busts,
        }, indent=2))
        return 0

    total_in = totals["input"] + totals["cache_read"] + totals["cache_write"]
    cache_pct = (totals["cache_read"] / total_in * 100) if total_in else 0.0

    print(f"Claude Code usage — last {args.days} day(s)")
    if args.project:
        print(f"Filter: project contains {args.project!r}")
    print("-" * 72)
    print(f"Turns:        {totals['turns']:,}")
    print(f"Input:        {fmt_tokens(totals['input'])} fresh  +  "
          f"{fmt_tokens(totals['cache_read'])} cache-read  +  "
          f"{fmt_tokens(totals['cache_write'])} cache-write")
    print(f"Output:       {fmt_tokens(totals['output'])}")
    print(f"Cache hit:    {cache_pct:.1f}% of input tokens came from cache")
    print(f"Estimated $:  ~${totals['cost']:.2f} (API list prices; subscription is flat)")
    print()

    def top(d, n=8):
        return sorted(d.items(), key=lambda kv: kv[1]["cost"], reverse=True)[:n]

    if by_model:
        print("By model (top by cost):")
        for name, row in top(by_model):
            print_row(name, row, width=36)
        print()
    if by_project:
        print("By project (top by cost):")
        for name, row in top(by_project):
            print_row(name, row, width=50)
        print()
    if by_day:
        print("By day:")
        for name, row in sorted(by_day.items()):
            print_row(name, row, width=12)
        print()
    if by_tool:
        print("By tool (pro-rated turn cost across tool_use calls):")
        for name, row in top(by_tool, n=12):
            print_row(name, row, width=24)
        print()
    if cache_busts:
        print(f"Cache-bust events ({len(cache_busts)} detected — prior cache invalidated):")
        for ev in cache_busts[:8]:
            tools = ",".join(ev["tools"]) or "(no tools)"
            prev_tools = ",".join(ev["prev_tools"]) or "(no tools)"
            print(
                f"  {ev['when']}  drop={fmt_tokens(ev['drop'])}  "
                f"new_write={fmt_tokens(ev['this_cache_write'])}  "
                f"this_turn_tools=[{tools}]  prev_turn_tools=[{prev_tools}]"
            )
        if len(cache_busts) > 8:
            print(f"  ... {len(cache_busts) - 8} more (use --json for full list)")
        print()

    # Heuristic callouts
    print("Heuristics:")
    if totals["turns"] and cache_pct < 60:
        print(f"  - Cache hit is {cache_pct:.1f}% — low. You may be starting fresh chats too often, "
              f"or editing files in ways that bust cache. Batch related work in one session.")
    else:
        print(f"  - Cache hit is {cache_pct:.1f}% — healthy.")
    opus_cost = sum(r["cost"] for m, r in by_model.items() if "opus" in m.lower())
    if totals["cost"] and opus_cost / totals["cost"] > 0.5:
        print(f"  - Opus is {opus_cost/totals['cost']*100:.0f}% of spend. "
              f"Consider Haiku (/model claude-haiku-4-5) for simple edits, renames, drafts.")
    if by_tool and totals["cost"]:
        top_tool, top_row = max(by_tool.items(), key=lambda kv: kv[1]["cost"])
        share = top_row["cost"] / totals["cost"] * 100
        if share > 25:
            print(f"  - Tool {top_tool!r} drives {share:.0f}% of pro-rated cost. "
                  f"Consider scoping its calls or disabling if rarely useful.")
    if cache_busts:
        print(f"  - {len(cache_busts)} cache-bust event(s) detected. "
              f"Each invalidates the prompt cache and re-pays input cost. "
              f"Common causes: switching projects mid-session, large file edits, "
              f"`/clear` followed by paste, or MCP server reload.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
