# claude-goal

A Codex-style `/goal` command for Claude Code, **safe across concurrent sessions**.

It gives Claude Code a persistent local goal state, Codex-inspired continuation instructions, pause/resume/clear/status controls, completion-audit guardrails, and a Stop hook that keeps Claude working while a goal is active.

## Why

Most goal-tracking tools key state by terminal id or working directory, which silently collapses every Claude Code conversation started from the same shell onto a single goal: clearing one wipes the others; a stop-hook block in tab A fires while tab B is on a totally different task.

`claude-goal` anchors each goal to the actual Claude Code session id (delivered to hooks via stdin JSON) by way of a marker file written at `SessionStart`. Subprocesses spawned during the session walk their ancestor PIDs to find the marker, so no env-var threading is needed and concurrent sessions stay fully isolated.

## Install

```bash
git clone https://github.com/<you>/claude-goal.git
cd claude-goal
./install.sh
```

This installs:

- `~/.claude/skills/goal` as a symlink to this repo's `goal/` directory
- a user-level Claude Code `Stop` hook in `~/.claude/settings.json`
- `SessionStart` and `SessionEnd` hooks that anchor each Claude conversation as its own session

State is stored at:

```text
~/.claude/goal/goals.sqlite
~/.claude/goal/sessions/<claude_pid>.id   # per-session anchor markers
```

After install, **restart Claude Code** so the `SessionStart` hook fires for currently-running sessions.

## Usage

```text
/goal find and fix the flaky auth tests
/goal --tokens 250K do deep research and build the full prototype
/goal
/goal status
/goal pause
/goal resume
/goal clear
/goal complete
```

When a goal is active, the command returns a continuation prompt that wraps the goal text in `<objective>` and requires a completion audit before marking the goal complete.

## Concurrent sessions

Open Claude Code in two tabs at the same time. Set `/goal A` in tab 1 and `/goal B` in tab 2. Each tab sees only its own goal. `/goal pause` in tab 1 leaves tab 2 untouched. The Stop hook in tab 1 only blocks on goal A; the Stop hook in tab 2 only blocks on goal B.

How it works:

1. The `SessionStart` hook receives Claude's session id via stdin JSON and writes `~/.claude/goal/sessions/<claude_pid>.id`. The Claude PID is `os.getppid()` from inside the hook.
2. When `claude_goal.py` is later invoked by a Bash subprocess, it walks its ancestor PIDs (`ps -o ppid=`) and reads the first marker it finds. That gives it the real Claude session id — `claude:<uuid>` — which is unique per conversation.
3. The Stop hook also receives the session id directly via stdin and uses it as the primary candidate.
4. Goals are stored in SQLite keyed by the resolved session id (`UNIQUE(session_id)`).

Fallbacks (in order, if no marker is reachable): `CLAUDE_GOAL_SESSION_ID` env override, `TERM_SESSION_ID` / `ITERM_SESSION_ID`, `PWD` hash. Existing goals from the legacy cwd-keyed scheme are still resolvable during the migration window.

## Stale marker cleanup

`SessionStart` cleans up marker files whose owning PIDs are no longer alive. Manual cleanup:

```bash
python3 ~/.claude/skills/goal/scripts/claude_goal.py cleanup-markers
```

## Notes

Claude Code custom skills do not currently expose reliable live per-turn token usage to markdown commands. Token budgets are therefore stored and displayed as soft budgets. Elapsed-time tracking is local and persistent.

The Stop hook blocks Claude from stopping while the current goal is active. It stops blocking when you run `/goal pause`, `/goal clear`, or `/goal complete`.

By default, the runaway guard allows up to 500 Stop-hook continuations for a single active goal. That high default is intentional: `/goal` is meant for long-running work where Claude may need many turns to finish. If you want a stricter cap, set `CLAUDE_GOAL_MAX_STOP_CONTINUES` before launching Claude Code:

```bash
export CLAUDE_GOAL_MAX_STOP_CONTINUES=50
```

## Test

```bash
python3 -m pytest tests
```

