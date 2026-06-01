#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$HOME/.claude/skills" "$HOME/.claude/commands" "$HOME/.claude/goal/sessions"

ln -sfn "$ROOT/goal" "$HOME/.claude/skills/goal"
chmod +x "$ROOT/goal/scripts/claude_goal.py"

# Older installs may have left behind a slash-command shim. The skill is
# now exposed as /goal directly, so a shim would create duplicate help entries.
if [ -L "$HOME/.claude/commands/goal.md" ] && [ "$(readlink "$HOME/.claude/commands/goal.md")" = "$ROOT/goal.md" ]; then
  rm "$HOME/.claude/commands/goal.md"
fi

# Hooks reference the symlink path so reinstalls / repo moves don't break them.
SCRIPT_PATH="$HOME/.claude/skills/goal/scripts/claude_goal.py"

python3 - "$HOME/.claude/settings.json" "$SCRIPT_PATH" <<'PY'
import json
import sys
from pathlib import Path

settings_path = Path(sys.argv[1])
script_path = sys.argv[2]
settings_path.parent.mkdir(parents=True, exist_ok=True)
if settings_path.exists():
    data = json.loads(settings_path.read_text())
else:
    data = {}

hooks = data.setdefault("hooks", {})

# Drop any pre-existing claude_goal hook entries so we install fresh ones
# pointing at the stable symlink path. Match by substring "claude_goal.py"
# so legacy absolute-path installs are picked up too.
for event_name in ("Stop", "SessionStart", "SessionEnd"):
    if event_name not in hooks:
        continue
    cleaned = []
    for group in hooks[event_name]:
        kept_hooks = [
            h for h in group.get("hooks", [])
            if "claude_goal.py" not in (h.get("command") or "")
        ]
        if kept_hooks:
            new_group = dict(group)
            new_group["hooks"] = kept_hooks
            cleaned.append(new_group)
        elif group.get("hooks") != []:
            # Group existed but had only claude_goal hooks — drop the empty group entirely
            pass
        else:
            cleaned.append(group)
    if cleaned:
        hooks[event_name] = cleaned
    else:
        del hooks[event_name]

def append_hook(event_name, command, matcher=""):
    bucket = hooks.setdefault(event_name, [])
    entry = {"matcher": matcher, "hooks": [{"type": "command", "command": command}]}
    bucket.append(entry)

append_hook("Stop", f"python3 {script_path} stop-hook")
append_hook("SessionStart", f"python3 {script_path} session-start-hook")
append_hook("SessionEnd", f"python3 {script_path} session-end-hook")

settings_path.write_text(json.dumps(data, indent=2) + "\n")
PY

echo "Installed /goal for Claude Code."
echo "Skill:        $HOME/.claude/skills/goal -> $ROOT/goal"
echo "State DB:     $HOME/.claude/goal/goals.sqlite"
echo "Sessions dir: $HOME/.claude/goal/sessions/"
echo
echo "Hooks installed:"
echo "  Stop          python3 $SCRIPT_PATH stop-hook"
echo "  SessionStart  python3 $SCRIPT_PATH session-start-hook"
echo "  SessionEnd    python3 $SCRIPT_PATH session-end-hook"
echo
echo "Restart Claude Code to pick up the SessionStart hook for already-running sessions."
