#!/bin/bash
export DISPLAY=:0
exec /opt/google/chrome/chrome \
  --remote-debugging-port=9222 \
  --remote-allow-origins=http://127.0.0.1:9222 \
  --user-data-dir=$HOME/.config/google-chrome-cdp \
  --no-first-run \
  --disable-session-crashed-bubble \
  --restore-last-session
