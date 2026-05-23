# stop-claude-code-limits

Claude Code skill that turns the passive 11-rule advice into an active loop. Three modes share the same local JSONL parser (no network, no deps beyond stdlib).

| Mode | Script | When |
|------|--------|------|
| Report | `scripts/usage-report.py` | One-shot historical breakdown with tool attribution + cache-bust events |
| Live coach | `scripts/watch.py` | Long-running tail; per-turn dashboard + real-time `[ALERT]` lines |
| Stop-hook advisor | `scripts/coach-hook.py` | Fires once at session end with a single advisory if thresholds hit |

## Install

Clone anywhere. Skill loads from `SKILL.md`. To enable the Stop hook, merge `hooks.example.json` into `~/.claude/settings.json` and replace `<SKILL_DIR>` with the absolute path.

## Quick start

```bash
# Historical report (last 7 days)
python3 scripts/usage-report.py --days 7

# Live dashboard for the current project's newest session
python3 scripts/watch.py --from-start

# Dry-run the Stop hook against a transcript
echo '{"transcript_path":"/path/to/session.jsonl"}' | python3 scripts/coach-hook.py
```

## Tests

```bash
python3 scripts/test_usage_report.py
```
