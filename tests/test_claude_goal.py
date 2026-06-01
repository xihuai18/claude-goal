import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "goal" / "scripts" / "claude_goal.py"


def run_goal(tmp_path, *args, session="test-session", env_extra=None):
    env = os.environ.copy()
    env["CLAUDE_GOAL_HOME"] = str(tmp_path / "goal-home")
    env["CLAUDE_GOAL_DB"] = str(tmp_path / "goal-home" / "goals.sqlite")
    env["CLAUDE_GOAL_SESSION_ID"] = session
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _load_module(monkeypatch, tmp_path):
    """Import the script as a module so we can monkeypatch internals."""
    spec = importlib.util.spec_from_file_location("claude_goal_under_test", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STATE_DIR", tmp_path / "goal-home")
    monkeypatch.setattr(module, "DB_PATH", tmp_path / "goal-home" / "goals.sqlite")
    return module


# -------- core CRUD --------


def test_set_status_pause_resume_complete(tmp_path):
    result = run_goal(tmp_path, "invoke", "--tokens", "98.5K", "improve benchmark coverage")
    assert result.returncode == 0, result.stderr
    assert "Action: set" in result.stdout
    assert "Token budget: 98.5K" in result.stdout
    assert "<goal>" in result.stdout

    result = run_goal(tmp_path, "pause")
    assert result.returncode == 0, result.stderr
    assert "Status: paused" in result.stdout

    result = run_goal(tmp_path, "resume")
    assert result.returncode == 0, result.stderr
    assert "Status: active" in result.stdout

    result = run_goal(tmp_path, "complete")
    assert result.returncode == 0, result.stderr
    assert "Status: complete" in result.stdout


def test_rejects_empty_and_duplicate_without_replace(tmp_path):
    result = run_goal(tmp_path, "set")
    assert result.returncode == 1
    assert "goal objective must not be empty" in result.stderr

    assert run_goal(tmp_path, "set", "first objective").returncode == 0
    result = run_goal(tmp_path, "set", "second objective")
    assert result.returncode == 1
    assert "already has a goal" in result.stderr


def test_json_output(tmp_path):
    assert run_goal(tmp_path, "set", "ship the thing").returncode == 0
    result = run_goal(tmp_path, "json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["objective"] == "ship the thing"
    assert data["status"] == "active"


# -------- stop hook --------


def test_stop_hook_blocks_active_goal(tmp_path):
    assert run_goal(tmp_path, "set", "keep going").returncode == 0
    env = os.environ.copy()
    env["CLAUDE_GOAL_HOME"] = str(tmp_path / "goal-home")
    env["CLAUDE_GOAL_DB"] = str(tmp_path / "goal-home" / "goals.sqlite")
    env["CLAUDE_GOAL_SESSION_ID"] = "test-session"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "stop-hook"],
        input=json.dumps({"session_id": "test-session", "stop_hook_active": False}),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["decision"] == "block"
    assert "<goal>" in data["reason"]


def test_stop_hook_allows_paused_goal(tmp_path):
    assert run_goal(tmp_path, "set", "keep going").returncode == 0
    assert run_goal(tmp_path, "pause").returncode == 0
    env = os.environ.copy()
    env["CLAUDE_GOAL_HOME"] = str(tmp_path / "goal-home")
    env["CLAUDE_GOAL_DB"] = str(tmp_path / "goal-home" / "goals.sqlite")
    env["CLAUDE_GOAL_SESSION_ID"] = "test-session"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "stop-hook"],
        input=json.dumps({"session_id": "test-session"}),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


# -------- session isolation (legacy paths still work) --------


def test_cli_does_not_leak_goals_across_sessions(tmp_path):
    """A goal set in session A must NOT surface for session B."""
    assert run_goal(tmp_path, "set", "session A goal", session="session-a").returncode == 0

    status_b = run_goal(tmp_path, "status", session="session-b")
    assert status_b.returncode == 0, status_b.stderr
    assert "No goal is currently set" in status_b.stdout

    env = os.environ.copy()
    env["CLAUDE_GOAL_HOME"] = str(tmp_path / "goal-home")
    env["CLAUDE_GOAL_DB"] = str(tmp_path / "goal-home" / "goals.sqlite")
    env["CLAUDE_GOAL_SESSION_ID"] = "session-b"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "stop-hook"],
        input=json.dumps({"session_id": "session-b-claude", "cwd": "/different/path"}),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_term_session_anchors_goal_across_pwd_drift(tmp_path):
    """A goal set in one terminal session stays reachable across cwd drift."""
    env = os.environ.copy()
    env["CLAUDE_GOAL_HOME"] = str(tmp_path / "goal-home")
    env["CLAUDE_GOAL_DB"] = str(tmp_path / "goal-home" / "goals.sqlite")
    env.pop("CLAUDE_GOAL_SESSION_ID", None)
    env.pop("CLAUDE_SESSION_ID", None)
    env["TERM_SESSION_ID"] = "iterm-tab-abc-123"
    env["PWD"] = "/tmp/orig-cwd"

    set_result = subprocess.run(
        [sys.executable, str(SCRIPT), "set", "stay alive across drift"],
        env=env, text=True, capture_output=True, check=False,
    )
    assert set_result.returncode == 0, set_result.stderr

    env["PWD"] = "/tmp/wandered-far-away"
    status_result = subprocess.run(
        [sys.executable, str(SCRIPT), "status"],
        env=env, text=True, capture_output=True, check=False,
    )
    assert status_result.returncode == 0, status_result.stderr
    assert "stay alive across drift" in status_result.stdout
    assert "Status: active" in status_result.stdout


def test_stop_hook_finds_goal_via_hook_payload_cwd(tmp_path):
    """Stop hook resolves legacy cwd-keyed goals via the hook payload's cwd field."""
    real_cwd = "/Users/alice/proj-a"
    real_cwd_session_id = "cwd:" + hashlib.sha256(real_cwd.encode()).hexdigest()[:16]

    assert run_goal(tmp_path, "set", "keep going", session=real_cwd_session_id).returncode == 0

    env = os.environ.copy()
    env["CLAUDE_GOAL_HOME"] = str(tmp_path / "goal-home")
    env["CLAUDE_GOAL_DB"] = str(tmp_path / "goal-home" / "goals.sqlite")
    env["CLAUDE_GOAL_SESSION_ID"] = "drifted-subshell"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "stop-hook"],
        input=json.dumps({"session_id": "drifted-subshell-claude", "cwd": real_cwd}),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["decision"] == "block"
    assert "keep going" in data["reason"]


# -------- multi-session: SessionStart marker file --------


def _write_marker(state_dir: Path, pid: int, session_id: str) -> Path:
    sessions = state_dir / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    marker = sessions / f"{pid}.id"
    marker.write_text(json.dumps({"session_id": session_id, "cwd": "/anywhere", "claude_pid": pid}))
    return marker


def test_session_id_resolves_from_ancestor_marker(tmp_path):
    """When a SessionStart marker exists at an ancestor PID, session_id() returns claude:<id>."""
    state_dir = tmp_path / "goal-home"
    state_dir.mkdir()
    test_pid = os.getpid()
    _write_marker(state_dir, test_pid, "claude-session-XYZ")

    env = os.environ.copy()
    env["CLAUDE_GOAL_HOME"] = str(state_dir)
    env["CLAUDE_GOAL_DB"] = str(state_dir / "goals.sqlite")
    env.pop("CLAUDE_GOAL_SESSION_ID", None)
    env.pop("CLAUDE_SESSION_ID", None)
    env.pop("TERM_SESSION_ID", None)
    env.pop("ITERM_SESSION_ID", None)

    set_result = subprocess.run(
        [sys.executable, str(SCRIPT), "set", "anchored goal"],
        env=env, text=True, capture_output=True, check=False,
    )
    assert set_result.returncode == 0, set_result.stderr

    json_result = subprocess.run(
        [sys.executable, str(SCRIPT), "json"],
        env=env, text=True, capture_output=True, check=False,
    )
    assert json_result.returncode == 0, json_result.stderr
    data = json.loads(json_result.stdout)
    assert data["objective"] == "anchored goal"
    assert data["session_id"] == "claude:claude-session-XYZ"


def test_marker_anchor_isolates_concurrent_sessions(tmp_path, monkeypatch):
    """Two simulated Claude sessions, same cwd & no TERM, get isolated goals via markers."""
    state_dir = tmp_path / "goal-home"
    state_dir.mkdir()

    # Session A: explicit session id "claude:A"
    env_a = os.environ.copy()
    env_a["CLAUDE_GOAL_HOME"] = str(state_dir)
    env_a["CLAUDE_GOAL_DB"] = str(state_dir / "goals.sqlite")
    env_a["CLAUDE_GOAL_SESSION_ID"] = "claude:A"
    env_a.pop("CLAUDE_SESSION_ID", None)
    env_a.pop("TERM_SESSION_ID", None)
    env_a.pop("ITERM_SESSION_ID", None)
    env_a["PWD"] = "/Users/me"

    set_a = subprocess.run(
        [sys.executable, str(SCRIPT), "set", "goal A"],
        env=env_a, text=True, capture_output=True, check=False,
    )
    assert set_a.returncode == 0, set_a.stderr

    # Session B: different explicit session id, same cwd
    env_b = dict(env_a)
    env_b["CLAUDE_GOAL_SESSION_ID"] = "claude:B"

    status_b = subprocess.run(
        [sys.executable, str(SCRIPT), "status"],
        env=env_b, text=True, capture_output=True, check=False,
    )
    assert status_b.returncode == 0, status_b.stderr
    assert "No goal is currently set" in status_b.stdout, status_b.stdout
    assert "goal A" not in status_b.stdout

    set_b = subprocess.run(
        [sys.executable, str(SCRIPT), "set", "goal B"],
        env=env_b, text=True, capture_output=True, check=False,
    )
    assert set_b.returncode == 0, set_b.stderr

    # Each session sees its own goal
    status_a = subprocess.run(
        [sys.executable, str(SCRIPT), "status"],
        env=env_a, text=True, capture_output=True, check=False,
    )
    assert "goal A" in status_a.stdout
    assert "goal B" not in status_a.stdout

    status_b2 = subprocess.run(
        [sys.executable, str(SCRIPT), "status"],
        env=env_b, text=True, capture_output=True, check=False,
    )
    assert "goal B" in status_b2.stdout
    assert "goal A" not in status_b2.stdout


def test_clear_in_one_session_does_not_touch_the_other(tmp_path):
    """Regression for the bug that motivated this fork: /goal clear in tab A wiped tab B."""
    state_dir = tmp_path / "goal-home"
    state_dir.mkdir()

    env_a = os.environ.copy()
    env_a["CLAUDE_GOAL_HOME"] = str(state_dir)
    env_a["CLAUDE_GOAL_DB"] = str(state_dir / "goals.sqlite")
    env_a["CLAUDE_GOAL_SESSION_ID"] = "claude:A"
    env_a.pop("CLAUDE_SESSION_ID", None)
    env_a.pop("TERM_SESSION_ID", None)
    env_a.pop("ITERM_SESSION_ID", None)
    env_a["PWD"] = "/Users/me"
    env_b = dict(env_a)
    env_b["CLAUDE_GOAL_SESSION_ID"] = "claude:B"

    assert subprocess.run(
        [sys.executable, str(SCRIPT), "set", "alpha"],
        env=env_a, text=True, capture_output=True, check=False,
    ).returncode == 0

    assert subprocess.run(
        [sys.executable, str(SCRIPT), "set", "beta"],
        env=env_b, text=True, capture_output=True, check=False,
    ).returncode == 0

    clear_a = subprocess.run(
        [sys.executable, str(SCRIPT), "clear"],
        env=env_a, text=True, capture_output=True, check=False,
    )
    assert clear_a.returncode == 0, clear_a.stderr
    assert "Goal cleared" in clear_a.stdout

    status_b = subprocess.run(
        [sys.executable, str(SCRIPT), "status"],
        env=env_b, text=True, capture_output=True, check=False,
    )
    assert "beta" in status_b.stdout, f"Session B's goal was wiped by Session A's clear: {status_b.stdout!r}"

    status_a = subprocess.run(
        [sys.executable, str(SCRIPT), "status"],
        env=env_a, text=True, capture_output=True, check=False,
    )
    assert "alpha" not in status_a.stdout
    assert "No goal is currently set" in status_a.stdout


# -------- SessionStart / SessionEnd hook commands --------


def test_session_start_hook_writes_marker(tmp_path):
    state_dir = tmp_path / "goal-home"
    env = os.environ.copy()
    env["CLAUDE_GOAL_HOME"] = str(state_dir)
    env["CLAUDE_GOAL_DB"] = str(state_dir / "goals.sqlite")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "session-start-hook"],
        input=json.dumps({"session_id": "claude-real-session-123", "cwd": "/Users/me"}),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    sessions_dir = state_dir / "sessions"
    files = list(sessions_dir.glob("*.id"))
    assert len(files) == 1, f"Expected one marker, found {files}"
    payload = json.loads(files[0].read_text())
    assert payload["session_id"] == "claude-real-session-123"
    assert payload["cwd"] == "/Users/me"


def test_session_start_hook_ignores_missing_session_id(tmp_path):
    state_dir = tmp_path / "goal-home"
    env = os.environ.copy()
    env["CLAUDE_GOAL_HOME"] = str(state_dir)
    env["CLAUDE_GOAL_DB"] = str(state_dir / "goals.sqlite")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "session-start-hook"],
        input="{}",
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    sessions_dir = state_dir / "sessions"
    if sessions_dir.exists():
        assert list(sessions_dir.glob("*.id")) == []


def test_session_end_hook_removes_marker(tmp_path):
    state_dir = tmp_path / "goal-home"
    env = os.environ.copy()
    env["CLAUDE_GOAL_HOME"] = str(state_dir)
    env["CLAUDE_GOAL_DB"] = str(state_dir / "goals.sqlite")

    # Start
    subprocess.run(
        [sys.executable, str(SCRIPT), "session-start-hook"],
        input=json.dumps({"session_id": "abc"}),
        env=env, text=True, capture_output=True, check=False,
    )
    sessions_dir = state_dir / "sessions"
    assert list(sessions_dir.glob("*.id"))

    # End — invoked from the same parent so ppid matches
    end = subprocess.run(
        [sys.executable, str(SCRIPT), "session-end-hook"],
        input=json.dumps({"session_id": "abc"}),
        env=env, text=True, capture_output=True, check=False,
    )
    assert end.returncode == 0, end.stderr
    assert list(sessions_dir.glob("*.id")) == []


def test_cleanup_markers_removes_dead_pids(tmp_path):
    state_dir = tmp_path / "goal-home"
    sessions = state_dir / "sessions"
    sessions.mkdir(parents=True)
    # PID 0 is never alive
    (sessions / "0.id").write_text(json.dumps({"session_id": "ghost"}))
    # PID 1 (init) is always alive on macOS/Linux — should NOT be removed
    (sessions / "1.id").write_text(json.dumps({"session_id": "init"}))

    env = os.environ.copy()
    env["CLAUDE_GOAL_HOME"] = str(state_dir)
    env["CLAUDE_GOAL_DB"] = str(state_dir / "goals.sqlite")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "cleanup-markers"],
        env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not (sessions / "0.id").exists()
    assert (sessions / "1.id").exists()


# -------- direct module-level helper tests --------


def test_session_id_priority_order(monkeypatch, tmp_path):
    cg = _load_module(monkeypatch, tmp_path)

    # Strip everything
    for k in ("CLAUDE_GOAL_SESSION_ID", "CLAUDE_SESSION_ID",
              "TERM_SESSION_ID", "ITERM_SESSION_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PWD", "/some/pwd")

    # 1. Falls back to cwd hash when nothing else is set
    sid = cg.session_id()
    assert sid.startswith("cwd:")

    # 2. TERM beats cwd
    monkeypatch.setenv("TERM_SESSION_ID", "iterm-tab-1")
    sid = cg.session_id()
    assert sid.startswith("term:")

    # 3. Marker beats TERM
    state_dir = tmp_path / "goal-home"
    _write_marker(state_dir, os.getpid(), "claude-XYZ")
    # session_id() walks getppid(), not getpid(). For unit test, also write at ppid.
    _write_marker(state_dir, os.getppid(), "claude-XYZ")
    sid = cg.session_id()
    assert sid == "claude:claude-XYZ"

    # 4. Explicit env override beats marker
    monkeypatch.setenv("CLAUDE_GOAL_SESSION_ID", "explicit-override")
    sid = cg.session_id()
    assert sid == "explicit-override"


def test_walk_ancestors_handles_dead_chain(monkeypatch, tmp_path):
    """If no marker exists in the ancestor chain, walker returns None cleanly."""
    cg = _load_module(monkeypatch, tmp_path)
    # No markers written
    assert cg._walk_ancestors_for_session() is None


# -------- find_goal priority ordering --------


def test_find_goal_prefers_higher_priority_candidate(tmp_path):
    """find_goal returns the first match in candidate order, not max(updated_at)."""
    state_dir = tmp_path / "goal-home"

    # Set a goal under cwd hash (lower priority)
    cwd_sid = "cwd:" + hashlib.sha256(b"/Users/me/proj").hexdigest()[:16]
    env_base = os.environ.copy()
    env_base["CLAUDE_GOAL_HOME"] = str(state_dir)
    env_base["CLAUDE_GOAL_DB"] = str(state_dir / "goals.sqlite")

    env_cwd = dict(env_base)
    env_cwd["CLAUDE_GOAL_SESSION_ID"] = cwd_sid
    assert subprocess.run(
        [sys.executable, str(SCRIPT), "set", "legacy cwd goal"],
        env=env_cwd, text=True, capture_output=True, check=False,
    ).returncode == 0

    # Set a goal under claude:session (higher priority)
    env_claude = dict(env_base)
    env_claude["CLAUDE_GOAL_SESSION_ID"] = "claude:primary-session"
    assert subprocess.run(
        [sys.executable, str(SCRIPT), "set", "real session goal"],
        env=env_claude, text=True, capture_output=True, check=False,
    ).returncode == 0

    # Now query with candidate list [claude:primary-session, cwd:<hash>]
    # Should find the claude: goal even if cwd goal has a newer updated_at.
    import time
    time.sleep(0.1)
    # Touch the cwd goal to make its updated_at newer
    subprocess.run(
        [sys.executable, str(SCRIPT), "pause"],
        env=env_cwd, text=True, capture_output=True, check=False,
    )
    subprocess.run(
        [sys.executable, str(SCRIPT), "resume"],
        env=env_cwd, text=True, capture_output=True, check=False,
    )

    # Status from the claude: session should still show its own goal
    status = subprocess.run(
        [sys.executable, str(SCRIPT), "status"],
        env=env_claude, text=True, capture_output=True, check=False,
    )
    assert "real session goal" in status.stdout
    assert "legacy cwd goal" not in status.stdout


# -------- session_end_hook pauses active goal --------


def test_session_end_hook_pauses_active_goal(tmp_path):
    """When a session ends, its active goal is paused to prevent zombie."""
    state_dir = tmp_path / "goal-home"
    env = os.environ.copy()
    env["CLAUDE_GOAL_HOME"] = str(state_dir)
    env["CLAUDE_GOAL_DB"] = str(state_dir / "goals.sqlite")
    env["CLAUDE_GOAL_SESSION_ID"] = "claude:ending-session"

    # Set and verify active
    assert subprocess.run(
        [sys.executable, str(SCRIPT), "set", "will be paused on end"],
        env=env, text=True, capture_output=True, check=False,
    ).returncode == 0

    # Fire session-end-hook
    end_result = subprocess.run(
        [sys.executable, str(SCRIPT), "session-end-hook"],
        input=json.dumps({"session_id": "ending-session", "cwd": "/somewhere"}),
        env=env, text=True, capture_output=True, check=False,
    )
    assert end_result.returncode == 0, end_result.stderr

    # Verify the goal is now paused
    status = subprocess.run(
        [sys.executable, str(SCRIPT), "status"],
        env=env, text=True, capture_output=True, check=False,
    )
    assert "Status: paused" in status.stdout
    assert "will be paused on end" in status.stdout


def test_session_end_zombie_doesnt_block_other_sessions(tmp_path):
    """A goal paused by session end does NOT trigger stop hook for another session."""
    state_dir = tmp_path / "goal-home"
    env_a = os.environ.copy()
    env_a["CLAUDE_GOAL_HOME"] = str(state_dir)
    env_a["CLAUDE_GOAL_DB"] = str(state_dir / "goals.sqlite")
    env_a["CLAUDE_GOAL_SESSION_ID"] = "claude:session-a"
    env_a["PWD"] = "/Users/me/shared-project"

    # Session A sets a goal and then ends
    assert subprocess.run(
        [sys.executable, str(SCRIPT), "set", "session a work"],
        env=env_a, text=True, capture_output=True, check=False,
    ).returncode == 0

    subprocess.run(
        [sys.executable, str(SCRIPT), "session-end-hook"],
        input=json.dumps({"session_id": "session-a", "cwd": "/Users/me/shared-project"}),
        env=env_a, text=True, capture_output=True, check=False,
    )

    # Session B in the same directory — stop hook should NOT block
    env_b = dict(env_a)
    env_b["CLAUDE_GOAL_SESSION_ID"] = "claude:session-b"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "stop-hook"],
        input=json.dumps({"session_id": "session-b", "cwd": "/Users/me/shared-project"}),
        env=env_b, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""  # No blocking


# -------- stop hook max continues --------


def test_stop_hook_allows_stop_after_max_continues(tmp_path):
    """After max continues exceeded, stop hook outputs nothing (allows stop)."""
    env = os.environ.copy()
    env["CLAUDE_GOAL_HOME"] = str(tmp_path / "goal-home")
    env["CLAUDE_GOAL_DB"] = str(tmp_path / "goal-home" / "goals.sqlite")
    env["CLAUDE_GOAL_SESSION_ID"] = "test-session"
    env["CLAUDE_GOAL_MAX_STOP_CONTINUES"] = "2"

    # Set a goal
    assert subprocess.run(
        [sys.executable, str(SCRIPT), "set", "limited goal"],
        env=env, text=True, capture_output=True, check=False,
    ).returncode == 0

    # First 2 stop hooks should block
    for i in range(2):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "stop-hook"],
            input=json.dumps({"session_id": "test-session"}),
            env=env, text=True, capture_output=True, check=False,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["decision"] == "block", f"Iteration {i} should block"

    # 3rd stop hook should allow (no output)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "stop-hook"],
        input=json.dumps({"session_id": "test-session"}),
        env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0
    assert result.stdout == ""  # No blocking, no JSON output
