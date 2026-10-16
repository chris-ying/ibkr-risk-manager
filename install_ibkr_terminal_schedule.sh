#!/bin/bash
# Weekday launch checks only; does not stop a running risk manager.
set -euo pipefail

# ===== CHANGE ONLY THESE THREE SETTINGS =====
START_TIME="09:00"
END_TIME="16:00"           
INTERVAL_MINUTES=10
# ============================================

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCHER="$PROJECT_DIR/start_risk_manager_terminal.sh"
[[ -f "$PROJECT_DIR/risk_manager.py" ]] || { echo "Missing risk_manager.py in $PROJECT_DIR" >&2; exit 1; }
[[ -f "$LAUNCHER" ]] || { echo "Missing start_risk_manager_terminal.sh in $PROJECT_DIR" >&2; exit 1; }

# macOS's default Bash is 3.2, so use portable arithmetic and regex matching.
parse_time() {
  local value="$1" hh mm
  if [[ ! "$value" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
    echo "Invalid time '$value' (expected HH:MM in 24-hour format)" >&2
    exit 1
  fi
  hh="${value%:*}"; mm="${value#*:}"
  echo $((10#$hh * 60 + 10#$mm))
}
START_MINUTES="$(parse_time "$START_TIME")"
END_MINUTES="$(parse_time "$END_TIME")"
[[ "$INTERVAL_MINUTES" =~ ^[1-9][0-9]*$ ]] || { echo "INTERVAL_MINUTES must be a positive whole number" >&2; exit 1; }
(( END_MINUTES > START_MINUTES )) || { echo "END_TIME must be later than START_TIME on the same day" >&2; exit 1; }

if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
elif [[ -x "$PROJECT_DIR/venv/bin/python" ]]; then
  PYTHON_BIN="$PROJECT_DIR/venv/bin/python"
else
  PYTHON_BIN="$(command -v python3 || true)"
fi
[[ -n "$PYTHON_BIN" ]] || { echo "Python 3 not found" >&2; exit 1; }
if ! "$PYTHON_BIN" -c 'import ibapi' 2>/dev/null; then
  echo "WARNING: $PYTHON_BIN cannot import ibapi; check your Python environment." >&2
fi
printf -v py_quoted '%q' "$PYTHON_BIN"
printf 'IBKR_PYTHON=%s\n' "$py_quoted" > "$PROJECT_DIR/.ibkr_terminal_config"
chmod +x "$LAUNCHER"

AGENTS="$HOME/Library/LaunchAgents"
PLIST="$AGENTS/com.local.ibkrterminal.plist"
OLD_PLIST="$AGENTS/com.local.ibkrterminal.test.plist"
mkdir -p "$AGENTS" "$HOME/Library/Logs"
xml_escape() {
  local s="$1"
  s="${s//&/\&amp;}"; s="${s//</\&lt;}"; s="${s//>/\&gt;}"
  printf '%s' "$s"
}
LAUNCHER_XML="$(xml_escape "$LAUNCHER")"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.local.ibkrterminal</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$LAUNCHER_XML</string></array>
  <key>StartCalendarInterval</key><array>
PLIST
# launchd: Weekday 1=Monday, 5=Friday. End time is exclusive.
for weekday in 1 2 3 4 5; do
  for (( total=START_MINUTES; total<END_MINUTES; total+=INTERVAL_MINUTES )); do
    hour=$((total / 60))
    minute=$((total % 60))
    printf '    <dict><key>Weekday</key><integer>%d</integer><key>Hour</key><integer>%d</integer><key>Minute</key><integer>%d</integer></dict>\n' "$weekday" "$hour" "$minute" >> "$PLIST"
  done
done
cat >> "$PLIST" <<PLIST
  </array>
  <key>StandardOutPath</key><string>$(xml_escape "$HOME/Library/Logs/ibkr-terminal-scheduler.log")</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$HOME/Library/Logs/ibkr-terminal-scheduler-error.log")</string>
</dict></plist>
PLIST
/usr/bin/plutil -lint "$PLIST"
/bin/launchctl bootout "gui/$(id -u)" "$OLD_PLIST" 2>/dev/null || true
rm -f "$OLD_PLIST"
/bin/launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
/bin/launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed: Monday–Friday $START_TIME to $END_TIME, every $INTERVAL_MINUTES minutes."
