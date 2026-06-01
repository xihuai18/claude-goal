# claude-goal

A Codex-style `/goal` command for Claude Code, **safe across concurrent sessions**.

Gives Claude Code a persistent local goal state, auto-continuation via Stop hook, pause/resume/clear/complete controls, and completion-verification guardrails.

## Why

Most goal-tracking tools key state by terminal id or working directory, which silently collapses every Claude Code conversation started from the same shell onto a single goal: clearing one wipes the others; a stop-hook block in tab A fires while tab B is on a totally different task.

`claude-goal` anchors each goal to the actual Claude Code session id (delivered to hooks via stdin JSON) by way of a marker file written at `SessionStart`. Subprocesses spawned during the session walk their ancestor PIDs to find the marker, so no env-var threading is needed and concurrent sessions stay fully isolated.

## Install

**Via plugin marketplace (recommended):**

```
/plugin marketplace add xihuai18/claude-goal
/plugin install goal
```

**Via git clone:**

```bash
git clone https://github.com/xihuai18/claude-goal.git
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

When a goal is active, the Stop hook prevents Claude from stopping until you run `/goal pause`, `/goal clear`, or `/goal complete`.

Before marking complete, Claude verifies against real evidence (files, tests, output). If verification fails, work continues automatically.

## Concurrent sessions

Open Claude Code in two tabs at the same time. Set `/goal A` in tab 1 and `/goal B` in tab 2. Each tab sees only its own goal. `/goal pause` in tab 1 leaves tab 2 untouched. The Stop hook in tab 1 only blocks on goal A; the Stop hook in tab 2 only blocks on goal B.

How it works:

1. The `SessionStart` hook receives Claude's session id via stdin JSON and writes `~/.claude/goal/sessions/<claude_pid>.id`.
2. When `claude_goal.py` is later invoked by a Bash subprocess, it walks its ancestor PIDs and reads the first valid marker it finds. That gives it the real Claude session id — `claude:<uuid>` — which is unique per conversation.
3. The Stop hook receives the session id directly via stdin and uses it as the primary candidate.
4. Goals are stored in SQLite keyed by the resolved session id (`UNIQUE(session_id)`).
5. `find_goal` respects priority order: if a higher-priority anchor matches (even with a non-active goal), it stops searching — never falls through to a lower-priority cwd hash that might belong to another session.

Session end automatically pauses any active goal, preventing zombie goals from leaking across sessions.

Fallbacks (in order, if no marker is reachable): `CLAUDE_GOAL_SESSION_ID` env override, `TERM_SESSION_ID` / `ITERM_SESSION_ID`, `PWD` hash.

## Safety features

- **Fail-open hooks**: If the DB is corrupted or unreadable, the Stop hook allows Claude to stop naturally rather than crashing.
- **Marker TTL**: Markers older than 7 days are ignored to mitigate PID reuse by the OS.
- **Runaway guard**: Max 500 auto-continuations per goal (configurable via `CLAUDE_GOAL_MAX_STOP_CONTINUES`).
- **Events cleanup**: Events older than 30 days are pruned on each SessionStart.

## Test

```bash
python3 -m pytest tests -v
```

## Notes

Claude Code custom skills do not currently expose reliable live per-turn token usage to markdown commands. Token budgets are therefore stored and displayed as soft budgets. Elapsed-time tracking is local and persistent.

The Stop hook blocks Claude from stopping while the current goal is active. It stops blocking when you run `/goal pause`, `/goal clear`, or `/goal complete`.
