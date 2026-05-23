# stop-claude-code-limits

> A Claude Code **Skill** that shows you where your tokens actually go, warns you before you hit the limit, and auto-suggests `/compact` or a model switch — so you stop running into "usage limit reached."

Zero dependencies (Python stdlib only), fully local, never uploads anything.

---

## What problem does it solve?

Claude **re-reads the entire conversation history on every turn.** That means in a long chat, 98% of your tokens are spent re-reading what was already said, not generating new answers.

Consequences:
- You hit the limit fast and don't know why.
- You can't see which tool, project, or model is burning your budget.
- One broken cache invalidation, and the next turn pays full input cost again.

This tool reads the logs Claude Code already writes locally (`~/.claude/projects/*/*.jsonl`) and tells you:

- Which project, model, and day burned the most
- Which tool call drove the most spend (Bash? WebFetch? a specific MCP?)
- Your cache hit rate (anything below 60% is a problem)
- Which turn invalidated the prompt cache (cache-bust events)
- How much time you have left in the current session (live alerts)

---

## Three modes, pick one

| Mode | Command | Best for |
|------|---------|----------|
| **Report** | `python3 scripts/usage-report.py` | Reviewing the past week/month |
| **Live monitor** | `python3 scripts/watch.py --from-start` | Watching a dashboard while you work |
| **Auto-advisor** | Configure Stop hook (one-time setup) | Set-and-forget — only get a tip when something's off |

---

## Get started in 30 seconds

### 1. Clone the repo

```bash
git clone <your-repo-url> ~/stop-claude-code-limits
cd ~/stop-claude-code-limits
```

No `pip install`, no dependencies. Python 3.10+ is all you need.

### 2. Run a report (simplest)

```bash
python3 scripts/usage-report.py --days 7
```

Output looks like this:

```
Claude Code usage — last 7 day(s)
------------------------------------------------------------------------
Turns:        294
Input:        564 fresh  +  87.01M cache-read  +  4.10M cache-write
Output:       186.4K
Cache hit:    95.5% of input tokens came from cache
Estimated $:  ~$221.46  (API list prices; subscription is flat)

By model (top by cost):
  claude-opus-4-7      turns=  294  in=    564  out= 186.4K  cache_r= 87.01M (95.5%)  cache_w=  4.10M  ~$221.46

By tool (pro-rated turn cost across tool_use calls):
  Bash                 turns= 104  out=  30.6K  cache_w= 810.2K  ~$ 17.48
  TodoWrite            turns=   8  out=   4.9K  cache_w= 681.9K  ~$ 13.15
  ...

Cache-bust events (1 detected — prior cache invalidated):
  2026-05-23T01:53  drop=84.5K  new_write=90.7K  this_turn_tools=[Edit]

Heuristics:
  - Cache hit is 95.5% — healthy.
  - Opus is 100% of spend. Consider Haiku (/model claude-haiku-4-5) for simple edits.
  - 1 cache-bust event(s) detected.
```

**How to read it:**
- `Cache hit` < 60% → cache is being wasted; you're probably `/clear`-ing too often or switching projects mid-session
- `By tool` top entry → this tool is your biggest spend; ask if you really need every call
- `Cache-bust events` → these timestamps mark when an action wrecked the cache; learn from them

Common flags:

```bash
python3 scripts/usage-report.py --days 30                 # last 30 days
python3 scripts/usage-report.py --project myrepo          # scope to one project
python3 scripts/usage-report.py --days 7 --json           # JSON for further processing
```

---

### 3. Run the live dashboard (recommended)

Open a second terminal and run:

```bash
python3 scripts/watch.py --from-start
```

It auto-detects the newest session for the current project and prints a live dashboard while you work:

```
Watching /Users/you/.claude/projects/-Users-you-myrepo/abc123.jsonl
turns=  3  cache_hit= 87.9%  in=     16  out=     40  cache_r= 85.5K  5h_burn=$  1.66  total=$  1.66
turns=  4  cache_hit= 51.2%  ...
[ALERT] CACHE-BUST: cache_read dropped by 79.5K and 80.0K new cache_write. This turn's tools: [Bash].
[ALERT] LOW CACHE HIT: 38.5% (target >=60%). Likely cause: switching projects mid-session.
[ALERT] TURN COUNT: 20 turns. Run `/compact` to summarize in place.
```

**Field meanings:**

| Field | Meaning |
|-------|---------|
| `turns` | Number of conversation turns so far |
| `cache_hit` | Cache hit rate — higher is better (>60% is healthy) |
| `in` / `out` | Fresh input / output tokens this turn |
| `cache_r` | Tokens read back from cache |
| `5h_burn` | Estimated spend in the last 5 hours (Claude's billing window is a rolling 5h) |
| `total` | Cumulative cost for this session |

**Three alert types:**

- `[ALERT] TURN COUNT` — 20 turns reached; run `/compact` or `/clear`
- `[ALERT] LOW CACHE HIT` — cache hit dropped below 40%
- `[ALERT] CACHE-BUST` — cache just got broken **and the alert names the tool that did it**

Tune thresholds:

```bash
python3 scripts/watch.py --turn-warn 30 --cache-warn-pct 50 --bust-drop 50000
python3 scripts/watch.py --quiet         # alerts only, no dashboard
python3 scripts/watch.py --interval 3    # poll every 3 seconds
```

Press `Ctrl+C` to stop.

---

### 4. Configure the Stop hook (lowest effort)

Let Claude Code auto-check at the end of every session and print a tip if anything looks off.

Open `~/.claude/settings.json` and merge the `hooks` block from `hooks.example.json`. Replace `<SKILL_DIR>` with the absolute path to this repo (run `pwd` from the repo root to get it).

If your `settings.json` currently looks like this:

```json
{
  "theme": "dark",
  "model": "claude-opus-4-7"
}
```

Change it to:

```json
{
  "theme": "dark",
  "model": "claude-opus-4-7",
  "hooks": {
    "Stop": [
      {
        "command": "python3 /Users/you/stop-claude-code-limits/scripts/coach-hook.py",
        "description": "Usage coach"
      }
    ]
  }
}
```

Now, every time a session ends and any threshold is hit, you'll see a line like:

```
[usage-coach] 21 turns this session — run `/compact` to summarize | cache hit was only 38.5% | 2 cache-bust event(s) detected.
```

It never blocks Claude — it's advisory only.

Tune thresholds with environment variables (optional):

```bash
export COACH_TURN_WARN=30
export COACH_CACHE_WARN_PCT=50
export COACH_BUST_DROP=50000
```

---

## Install as a Claude Code Skill (advanced)

If you want Claude itself to auto-run this whenever you say "hit my limit" or "save tokens":

```bash
mkdir -p ~/.claude/skills/usage-limit-reducer
cp -r ./* ~/.claude/skills/usage-limit-reducer/
```

After that, Claude detects phrases like "running out of tokens," "this chat is getting long," "which model should I use," loads `SKILL.md`, and follows the diagnostic steps.

Trigger phrases: `hit my limit` / `usage limit` / `save tokens` / `coach` / `live monitor`.

---

## How it works (one sentence)

Claude Code writes a full `usage` block (input/output/cache_read/cache_creation/model/timestamp/tools) for every turn into `~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`. This tool just reads those files — no API calls.

Prices are from Anthropic's public API list (subscriptions don't bill per token, so the `$` figure is "what this would cost on the API," useful for relative comparison across sessions/tools/projects).

---

## FAQ

**Q: Does it really upload nothing?**
A: Only Python stdlib file I/O. No `requests`, no `urllib`. Grep the source — it's ~500 lines total.

**Q: I'm on a Pro/Max subscription with a flat monthly fee. Is the `$` number meaningful?**
A: Yes. It tells you "what this usage would cost at API list prices." Use it to compare relative cost across sessions, tools, and projects. Subscriptions have a rolling 5-hour window plus overall caps — lower relative cost means you're less likely to hit either.

**Q: Why is high `cache_hit` good?**
A: Cache reads cost 1/10 of fresh input (Opus: $1.50 vs $15 per million). A high hit rate means you're doing related work in one session without breaking the cache.

**Q: What triggers a `cache-bust`?**
A: Five common causes:
1. `/clear` mid-session followed by a big paste
2. Switching project directories (cwd changed)
3. Editing a large file that's referenced many times in context
4. Restarting an MCP server that emits large output
5. System prompt or `CLAUDE.md` changed

**Q: How do I run the tests?**
A: `python3 scripts/test_usage_report.py` — 20 test cases.

---

## File layout

```
stop-claude-code-limits/
├── SKILL.md                          # Skill entrypoint — Claude reads this
├── README.md                         # this file
├── hooks.example.json                # Stop hook config sample
└── scripts/
    ├── usage-report.py               # Report mode
    ├── watch.py                      # Live monitor
    ├── coach-hook.py                 # Stop hook script
    └── test_usage_report.py          # Unit tests
```

---

## License / Credits

Based on fifyrio's 11 rules for cutting Claude usage.
