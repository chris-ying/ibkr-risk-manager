#!/bin/bash
# Invoked by launchd every ten minutes in the configured window.
set -u
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$PROJECT_DIR/.ibkr_terminal_config"
if [[ ! -r "$CONFIG" ]]; then
  echo "Missing $CONFIG. Run install_ibkr_terminal_schedule.sh first." >&2
  exit 1
fi
# The installer generates these locally (no user input is executed remotely).
source "$CONFIG"

if [[ ! -f "$PROJECT_DIR/risk_manager.py" ]]; then
  echo "Cannot find $PROJECT_DIR/risk_manager.py" >&2
  exit 1
fi

# Check for an existing risk_manager.py process before touching Terminal.
# Unlike the older check, this also recognizes relative script paths and
# virtual-environment Python executables.
if /usr/bin/pgrep -f '(^|[[:space:]/])risk_manager[.]py([[:space:]]|$)' >/dev/null 2>&1; then
  exit 0
fi

# Quote strings as shell arguments so paths containing spaces work.
command_to_run="cd ~/ibkr-scripts && python3 -u risk_manager.py"

# Terminal is an interactive GUI app; the user must be logged in and Mac awake.
/usr/bin/osascript \
  -e 'on run argv' \
  -e 'tell application "Terminal"' \
  -e 'if (count of windows) > 0 then' \
  -e 'set targetTab to selected tab of front window' \
  -e 'if not (busy of targetTab) then' \
  -e 'do script (item 1 of argv) in targetTab' \
  -e 'else' \
  -e 'do script (item 1 of argv)' \
  -e 'end if' \
  -e 'else' \
  -e 'do script (item 1 of argv)' \
  -e 'end if' \
  -e 'end tell' \
  -e 'end run' \
  "$command_to_run"
