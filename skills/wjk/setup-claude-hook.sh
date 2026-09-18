#!/bin/sh
# Adds a Claude Code SessionStart hook that exports WEBJJONKU_SESSION_ID so
# every run in one Claude session reuses the same ChatGPT workspace Project.
# Idempotent. Requires jq at hook time. Takes effect in new sessions.
set -eu
SETTINGS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
python3 - "$SETTINGS" <<'EOF'
import json, os, sys
path = sys.argv[1]
cmd = ('[ -n "${CLAUDE_ENV_FILE-}" ] && jq -r '
       '\'"export WEBJJONKU_SESSION_ID=claude-" + .session_id\' '
       '>> "$CLAUDE_ENV_FILE"; exit 0')
data = json.load(open(path)) if os.path.exists(path) else {}
entries = data.setdefault("hooks", {}).setdefault("SessionStart", [])
if any("WEBJJONKU_SESSION_ID" in h.get("command", "") for e in entries for h in e.get("hooks", [])):
    print("hook already present:", path)
    sys.exit(0)
entries.append({"hooks": [{"type": "command", "command": cmd}]})
with open(path, "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
print("hook added:", path)
EOF
