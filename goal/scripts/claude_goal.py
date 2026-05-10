#!/usr/bin/env python3
"""Claude Code /goal — concurrent-session-safe.

Per-Claude-session goal state. Each Claude Code conversation gets its own
goal, even when several sessions run concurrently from the same working
directory.

Session anchoring (in priority order):
  1. CLAUDE_GOAL_SESSION_ID / CLAUDE_SESSION_ID (explicit override)
  2. SessionStart marker file at ~/.claude/goal/sessions/<claude_pid>.id
     (written by the SessionStart hook, discovered by walking ancestor PIDs)
  3. TERM_SESSION_ID / ITERM_SESSION_ID (terminal-tab anchor)
  4. PWD hash (last-resort fallback)

The script is intentionally dependency-free: Claude Code can execute it
from a skill or a slash command, and tests can run it directly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any


STATUSES = {"active", "paused", "budget_limited", "complete"}
MAX_OBJECTIVE_CHARS = 4000
STATE_DIR = Path(os.environ.get("CLAUDE_GOAL_HOME", Path.home() / ".claude" / "goal"))
DB_PATH = Path(os.environ.get("CLAUDE_GOAL_DB", STATE_DIR / "goals.sqlite"))
SESSIONS_SUBDIR = "sessions"
ANCESTOR_WALK_LIMIT = 20


def now() -> int:
    return int(time.time())


def _sessions_dir() -> Path:
    return STATE_DIR / SESSIONS_SUBDIR


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _parent_pid(pid: int) -> int | None:
    """Return the parent PID of `pid` via `ps`. None if unknown."""
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    raw = out.stdout.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _read_marker(pid: int) -> str | None:
    """Return the session id stored in the marker file at this pid, or None."""
    marker = _sessions_dir() / f"{pid}.id"
    try:
        data = json.loads(marker.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    sid = data.get("session_id")
    if not sid:
        return None
    return f"claude:{sid}"


def _walk_ancestors_for_session() -> str | None:
    """Walk parent PIDs looking for a SessionStart-written marker file.

    The SessionStart hook writes the Claude session id keyed by the Claude
    process's own PID. Any subprocess descended from that Claude session
    can find it by walking its ancestor PIDs and reading the marker file
    from each one.
    """
    try:
        pid = os.getppid()
    except OSError:
        return None
    for _ in range(ANCESTOR_WALK_LIMIT):
        if pid <= 1:
            return None
        sid = _read_marker(pid)
        if sid:
            return sid
        parent = _parent_pid(pid)
        if parent is None or parent == pid:
            return None
        pid = parent
    return None


def _term_session_id() -> str | None:
    """Stable identifier tied to the current terminal session.

    Bash subshells inherit TERM_SESSION_ID / ITERM_SESSION_ID, and the
    value is stable for the lifetime of the surrounding terminal tab.
    Useful as a fallback when SessionStart marker is missing (e.g. older
    Claude Code sessions that started before the hook was installed).
    """
    for key in ("TERM_SESSION_ID", "ITERM_SESSION_ID"):
        value = os.environ.get(key)
        if value:
            return "term:" + hashlib.sha256(value.encode()).hexdigest()[:16]
    return None


def session_id() -> str:
    """Pick the most stable session id available in the current process.

    Priority:
      1. CLAUDE_GOAL_SESSION_ID / CLAUDE_SESSION_ID — explicit override
      2. SessionStart marker file (per-Claude-session anchor)
      3. TERM_SESSION_ID / ITERM_SESSION_ID
      4. PWD hash (last resort; collides across concurrent sessions)
    """
    for key in ("CLAUDE_GOAL_SESSION_ID", "CLAUDE_SESSION_ID"):
        value = os.environ.get(key)
        if value:
            return value
    claude = _walk_ancestors_for_session()
    if claude:
        return claude
    term = _term_session_id()
    if term:
        return term
    cwd = os.environ.get("PWD") or str(Path.cwd())
    return "cwd:" + hashlib.sha256(cwd.encode()).hexdigest()[:16]


def cwd_session_id(cwd: str | None) -> str | None:
    if not cwd:
        return None
    return "cwd:" + hashlib.sha256(cwd.encode()).hexdigest()[:16]


def candidate_session_ids(hook_data: dict[str, Any] | None = None) -> list[str]:
    """De-duplicated session-id candidates ordered by preference.

    Includes every signal we have so a goal set under one anchor is still
    discoverable when the lookup fires under a slightly different one. The
    Claude-session marker comes first; the cwd hash comes last so it can
    still resolve legacy goals during the migration window.
    """
    out: list[str] = []
    sources: list[str | None] = [
        os.environ.get("CLAUDE_GOAL_SESSION_ID"),
        os.environ.get("CLAUDE_SESSION_ID"),
    ]
    if hook_data:
        hook_sid = hook_data.get("session_id")
        if hook_sid:
            sources.append(f"claude:{hook_sid}")
        sources.append(cwd_session_id(hook_data.get("cwd")))
    sources.append(_walk_ancestors_for_session())
    sources.append(_term_session_id())
    cwd = os.environ.get("PWD") or str(Path.cwd())
    sources.append("cwd:" + hashlib.sha256(cwd.encode()).hexdigest()[:16])
    sources.append(session_id())
    for value in sources:
        if value and value not in out:
            out.append(value)
    return out


def sqlite_connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS goals (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL UNIQUE,
            objective TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active', 'paused', 'budget_limited', 'complete')),
            token_budget INTEGER,
            tokens_used INTEGER NOT NULL DEFAULT 0,
            time_used_seconds INTEGER NOT NULL DEFAULT 0,
            active_started_at INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            completed_at INTEGER,
            source TEXT NOT NULL DEFAULT 'claude',
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_id TEXT,
            session_id TEXT NOT NULL,
            event TEXT NOT NULL,
            detail TEXT,
            created_at INTEGER NOT NULL
        );
        """
    )
    conn.commit()


def execute(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
    cur = conn.execute(sql, params)
    conn.commit()
    return cur


def event(conn: sqlite3.Connection, sid: str, event_name: str, detail: str | None = None, goal_id: str | None = None) -> None:
    execute(
        conn,
        "INSERT INTO events(goal_id, session_id, event, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (goal_id, sid, event_name, detail, now()),
    )


def fmt_elapsed(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, rem_minutes = divmod(minutes, 60)
    if hours >= 24:
        days, rem_hours = divmod(hours, 24)
        return f"{days}d {rem_hours}h {rem_minutes}m"
    return f"{hours}h" if rem_minutes == 0 else f"{hours}h {rem_minutes}m"


def fmt_tokens(value: int | None) -> str:
    if value is None:
        return "none"
    value = int(value)
    abs_value = abs(value)
    if abs_value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if abs_value >= 1_000:
        return f"{value / 1_000:.1f}K".replace(".0K", "K")
    return str(value)


def parse_tokens(text: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*", text)
    if not match:
        raise ValueError(f"invalid token budget: {text!r}")
    number = float(match.group(1))
    suffix = match.group(2).lower()
    multiplier = 1_000_000 if suffix == "m" else 1_000 if suffix == "k" else 1
    value = int(number * multiplier)
    if value <= 0:
        raise ValueError("goal budgets must be positive when provided")
    return value


def active_time(row: sqlite3.Row) -> int:
    used = int(row["time_used_seconds"] or 0)
    if row["status"] == "active" and row["active_started_at"]:
        used += max(0, now() - int(row["active_started_at"]))
    return used


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    data["current_time_used_seconds"] = active_time(row)
    data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
    return data


def get_goal(conn: sqlite3.Connection, sid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM goals WHERE session_id = ?", (sid,)).fetchone()


def find_goal(
    conn: sqlite3.Connection,
    candidates: list[str],
    *,
    only_active: bool = False,
) -> sqlite3.Row | None:
    """Find the goal that belongs to *this* session, robust to anchor drift.

    Tries each candidate session id and returns the most recently updated
    match. Critically, this does NOT fall back to "any active goal in the
    DB" — that fallback would leak goals across separate Claude sessions.
    With Claude-session marker + terminal anchor + cwd hash + env
    overrides, drift inside one session is handled, and no cross-session
    leakage is possible.
    """
    matches: list[sqlite3.Row] = []
    for sid in candidates:
        row = get_goal(conn, sid)
        if row and (not only_active or row["status"] == "active"):
            matches.append(row)
    if matches:
        return max(matches, key=lambda r: r["updated_at"] or 0)
    return None


def validate_objective(objective: str) -> str:
    objective = objective.strip()
    if not objective:
        raise ValueError("goal objective must not be empty")
    if len(objective) > MAX_OBJECTIVE_CHARS:
        raise ValueError(
            f"goal objective is too long: {len(objective)} characters. Limit: {MAX_OBJECTIVE_CHARS} characters. Put longer instructions in a file and refer to that file in the goal."
        )
    return objective


def set_goal(conn: sqlite3.Connection, sid: str, objective: str, token_budget: int | None) -> sqlite3.Row:
    objective = validate_objective(objective)
    existing = get_goal(conn, sid)
    if existing:
        raise ValueError("this Claude session already has a goal; use: /goal clear, then set a new goal")
    goal_id = str(uuid.uuid4())
    ts = now()
    status = "budget_limited" if token_budget is not None and token_budget <= 0 else "active"
    execute(
        conn,
        """
        INSERT INTO goals (
            id, session_id, objective, status, token_budget, tokens_used,
            time_used_seconds, active_started_at, created_at, updated_at,
            completed_at, source, metadata_json
        ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, ?, NULL, 'claude', '{}')
        ON CONFLICT(session_id) DO UPDATE SET
            id = excluded.id,
            objective = excluded.objective,
            status = excluded.status,
            token_budget = excluded.token_budget,
            tokens_used = 0,
            time_used_seconds = 0,
            active_started_at = excluded.active_started_at,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at,
            completed_at = NULL,
            source = excluded.source,
            metadata_json = excluded.metadata_json
        """,
        (goal_id, sid, objective, status, token_budget, ts, ts, ts),
    )
    event(conn, sid, "set", objective, goal_id)
    return get_goal(conn, sid)  # type: ignore[return-value]


def update_status(conn: sqlite3.Connection, sid: str, status: str) -> sqlite3.Row:
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status}")
    goal = find_goal(conn, candidate_session_ids())
    if not goal:
        raise ValueError("no goal is set for this Claude session")

    used = active_time(goal)
    ts = now()
    active_started_at = ts if status == "active" else None
    completed_at = ts if status == "complete" else goal["completed_at"]
    execute(
        conn,
        """
        UPDATE goals
        SET status = ?, time_used_seconds = ?, active_started_at = ?, updated_at = ?, completed_at = ?
        WHERE id = ?
        """,
        (status, used, active_started_at, ts, completed_at, goal["id"]),
    )
    event(conn, goal["session_id"], status, goal_id=goal["id"])
    return get_goal(conn, goal["session_id"])  # type: ignore[return-value]


def clear_goal(conn: sqlite3.Connection, sid: str) -> bool:
    goal = find_goal(conn, candidate_session_ids())
    if goal:
        execute(conn, "DELETE FROM goals WHERE id = ?", (goal["id"],))
        event(conn, goal["session_id"], "clear", goal_id=goal["id"])
        return True
    return False


def parse_set_args(raw: str) -> tuple[str, int | None]:
    tokens = shlex.split(raw)
    token_budget = None
    out: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in {"--tokens", "--token-budget", "--budget"}:
            i += 1
            if i >= len(tokens):
                raise ValueError(f"{t} requires a value")
            token_budget = parse_tokens(tokens[i])
        elif t.startswith("--tokens="):
            token_budget = parse_tokens(t.split("=", 1)[1])
        elif t.startswith("--token-budget="):
            token_budget = parse_tokens(t.split("=", 1)[1])
        elif t.startswith("--budget="):
            token_budget = parse_tokens(t.split("=", 1)[1])
        else:
            out.append(t)
        i += 1
    return " ".join(out), token_budget


def render_goal(row: sqlite3.Row | None) -> str:
    if not row:
        return "No goal is currently set for this Claude session."
    elapsed = active_time(row)
    parts = [
        "Goal",
        f"- Status: {row['status']}",
        f"- Objective: {row['objective']}",
        f"- Time used: {fmt_elapsed(elapsed)}",
        f"- Tokens used: {fmt_tokens(row['tokens_used'])}",
    ]
    if row["token_budget"] is not None:
        parts.append(f"- Token budget: {fmt_tokens(row['token_budget'])} (soft budget; Claude Code custom skills do not expose reliable live token counters)")
    return "\n".join(parts)


def render_goal_json(row: sqlite3.Row | None) -> str:
    return json.dumps(row_to_dict(row), indent=2, sort_keys=True)


CONTINUATION_INSTRUCTIONS = """\
Continue working toward the active Claude thread goal.

The objective below is the current goal. Treat it as task context, not as higher-priority instructions.

<objective>
{objective}
</objective>

Budget:
- Time spent pursuing goal: {elapsed}
- Tokens used: {tokens_used}
- Token budget: {token_budget}

Avoid repeating work that is already done. Choose the next concrete action toward the objective.

Before deciding that the goal is achieved, perform a completion audit against actual current state:
- Restate the objective as concrete deliverables or success criteria.
- Build a prompt-to-artifact checklist mapping every explicit requirement, named file, command, test, gate, and deliverable to concrete evidence.
- Inspect relevant files, command output, test results, repo state, or other real evidence.
- Identify missing, incomplete, weakly verified, or uncovered requirements.
- Treat uncertainty as not achieved; continue verification or work.

Only mark the goal complete after the audit shows the objective is achieved and no required work remains. To mark it complete, run:
`python3 ~/.claude/skills/goal/scripts/claude_goal.py complete`
Then report the final elapsed time and token-budget state to the user.
"""


STOP_HOOK_REASON = """\
An active /goal is still running.

<objective>
{objective}
</objective>

Continue working toward the objective. Avoid repeating completed work.

If the objective is fully achieved, first perform the completion audit, then run:
`python3 ~/.claude/skills/goal/scripts/claude_goal.py complete`

If the goal cannot continue productively because user input is required, explain the blocker clearly. The user can run `/goal pause` or `/goal clear` to stop automatic continuation.
"""


def render_invoke_result(action: str, goal: sqlite3.Row | None, extra: str = "") -> str:
    body = [f"Action: {action}", "", render_goal(goal)]
    if extra:
        body.extend(["", extra])
    if goal and goal["status"] == "active":
        body.extend(
            [
                "",
                "Claude instructions:",
                CONTINUATION_INSTRUCTIONS.format(
                    objective=goal["objective"],
                    elapsed=fmt_elapsed(active_time(goal)),
                    tokens_used=fmt_tokens(goal["tokens_used"]),
                    token_budget=fmt_tokens(goal["token_budget"]),
                ),
            ]
        )
    elif goal and goal["status"] == "paused":
        body.extend(["", "Claude instructions: Do not continue this goal until the user runs `/goal resume`."])
    elif goal and goal["status"] == "budget_limited":
        body.extend(["", "Claude instructions: The soft budget is exhausted; summarize progress and ask before continuing."])
    return "\n".join(body)


def invoke(raw_args: str) -> str:
    sid = session_id()
    with sqlite_connect() as conn:
        raw_args = (raw_args or "").strip()
        command = raw_args.split(maxsplit=1)[0].lower() if raw_args else "status"
        rest = raw_args.split(maxsplit=1)[1] if " " in raw_args else ""

        if command in {"status", "show", "get", "menu"}:
            return render_invoke_result("status", find_goal(conn, candidate_session_ids()))
        if command == "pause":
            return render_invoke_result("pause", update_status(conn, sid, "paused"))
        if command == "resume":
            return render_invoke_result("resume", update_status(conn, sid, "active"))
        if command == "clear":
            cleared = clear_goal(conn, sid)
            return "Goal cleared." if cleared else "No goal to clear."
        if command == "complete":
            return render_invoke_result("complete", update_status(conn, sid, "complete"))
        objective, budget = parse_set_args(raw_args)
        return render_invoke_result("set", set_goal(conn, sid, objective, budget))


def cleanup_stale_markers() -> int:
    """Remove session marker files whose owning PID is no longer alive."""
    sessions = _sessions_dir()
    if not sessions.exists():
        return 0
    removed = 0
    for path in sessions.glob("*.id"):
        try:
            pid = int(path.stem)
        except ValueError:
            continue
        if not _pid_alive(pid):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def session_start_hook() -> int:
    """Persist the Claude session id keyed by Claude's own PID.

    Claude Code invokes SessionStart hooks as direct children, so
    os.getppid() == the Claude process PID. Subprocesses spawned later
    in the session walk their ancestor PIDs to find this marker.
    """
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        data = {}
    sid = data.get("session_id")
    if not sid:
        return 0
    sessions = _sessions_dir()
    sessions.mkdir(parents=True, exist_ok=True)
    cleanup_stale_markers()
    claude_pid = os.getppid()
    marker = sessions / f"{claude_pid}.id"
    payload = {
        "session_id": sid,
        "cwd": data.get("cwd"),
        "claude_pid": claude_pid,
        "created_at": now(),
    }
    try:
        marker.write_text(json.dumps(payload))
    except OSError:
        return 0
    return 0


def session_end_hook() -> int:
    """Remove this session's marker file on shutdown."""
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        data = {}
    claude_pid = os.getppid()
    marker = _sessions_dir() / f"{claude_pid}.id"
    try:
        marker.unlink()
    except (FileNotFoundError, OSError):
        pass
    cleanup_stale_markers()
    return 0


def stop_hook() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        data = {}

    candidates = candidate_session_ids(data)

    with sqlite_connect() as conn:
        goal = find_goal(conn, candidates, only_active=True)
        if not goal or goal["status"] != "active":
            return 0

        max_continues = int(os.environ.get("CLAUDE_GOAL_MAX_STOP_CONTINUES", "500"))
        recent_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM events
            WHERE goal_id = ?
              AND event = 'stop_continue'
              AND created_at >= ?
            """,
            (goal["id"], goal["active_started_at"] or goal["created_at"]),
        ).fetchone()[0]
        if recent_count >= max_continues:
            print(
                json.dumps(
                    {
                        "continue": True,
                        "stopReason": f"/goal auto-continuation stopped after {max_continues} Stop-hook continuations. Run /goal resume or raise CLAUDE_GOAL_MAX_STOP_CONTINUES to continue automatically.",
                    }
                )
            )
            return 0

        event(conn, goal["session_id"], "stop_continue", goal_id=goal["id"])
        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": STOP_HOOK_REASON.format(objective=goal["objective"]),
                }
            )
        )
        return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] in {"invoke", "set"}:
        cmd = argv[0]
        raw = " ".join(argv[1:])
        try:
            if cmd == "invoke":
                print(invoke(raw))
            else:
                objective, budget = parse_set_args(raw)
                with sqlite_connect() as conn:
                    print(render_invoke_result("set", set_goal(conn, session_id(), objective, budget)))
        except Exception as exc:
            print(f"goal error: {exc}", file=sys.stderr)
            return 1
        return 0

    parser = argparse.ArgumentParser(description="Claude Code /goal command")
    sub = parser.add_subparsers(dest="cmd")
    p_invoke = sub.add_parser("invoke", help="Process slash-command arguments and print Claude-facing instructions")
    p_invoke.add_argument("args", nargs=argparse.REMAINDER)
    sub.add_parser("status")
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("clear")
    sub.add_parser("complete")
    p_set = sub.add_parser("set")
    p_set.add_argument("args", nargs=argparse.REMAINDER)
    p_json = sub.add_parser("json")
    p_json.add_argument("--session-id", default=session_id())
    sub.add_parser("stop-hook")
    sub.add_parser("session-start-hook")
    sub.add_parser("session-end-hook")
    sub.add_parser("cleanup-markers")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "invoke":
            print(invoke(" ".join(args.args)))
        elif args.cmd == "status":
            with sqlite_connect() as conn:
                print(render_invoke_result("status", find_goal(conn, candidate_session_ids())))
        elif args.cmd == "pause":
            with sqlite_connect() as conn:
                print(render_invoke_result("pause", update_status(conn, session_id(), "paused")))
        elif args.cmd == "resume":
            with sqlite_connect() as conn:
                print(render_invoke_result("resume", update_status(conn, session_id(), "active")))
        elif args.cmd == "clear":
            with sqlite_connect() as conn:
                print("Goal cleared." if clear_goal(conn, session_id()) else "No goal to clear.")
        elif args.cmd == "complete":
            with sqlite_connect() as conn:
                print(render_invoke_result("complete", update_status(conn, session_id(), "complete")))
        elif args.cmd == "set":
            objective, budget = parse_set_args(" ".join(args.args))
            with sqlite_connect() as conn:
                print(render_invoke_result("set", set_goal(conn, session_id(), objective, budget)))
        elif args.cmd == "json":
            with sqlite_connect() as conn:
                if args.session_id != session_id():
                    print(render_goal_json(get_goal(conn, args.session_id)))
                else:
                    print(render_goal_json(find_goal(conn, candidate_session_ids())))
        elif args.cmd == "stop-hook":
            return stop_hook()
        elif args.cmd == "session-start-hook":
            return session_start_hook()
        elif args.cmd == "session-end-hook":
            return session_end_hook()
        elif args.cmd == "cleanup-markers":
            removed = cleanup_stale_markers()
            print(f"Removed {removed} stale marker file(s).")
        else:
            parser.print_help()
            return 2
    except Exception as exc:
        print(f"goal error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
