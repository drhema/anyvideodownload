#!/bin/bash
# Start Xvfb on DISPLAY=:99 so Chromium can run "headed" without a monitor.
# Then exec the given command (pyvid-api or pyvid CLI).
set -euo pipefail

: "${DISPLAY:=:99}"

# Start Xvfb in background, silence its output
Xvfb "$DISPLAY" -screen 0 1280x800x24 -ac +extension GLX +render -noreset > /dev/null 2>&1 &
XVFB_PID=$!

# Give Xvfb a moment to initialize
for _ in 1 2 3 4 5; do
  if xdpyinfo -display "$DISPLAY" > /dev/null 2>&1; then break; fi
  sleep 0.2
done

# Propagate SIGTERM/SIGINT to both Xvfb and the app
cleanup() {
  if kill -0 "$XVFB_PID" 2>/dev/null; then
    kill "$XVFB_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

exec "$@"
