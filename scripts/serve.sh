#!/bin/bash
# Launch wrapper for the BiliAnnotator Flask app, run by launchd.
# Bound to the LAN and caffeinated so the Mac does not sleep and drop phones.
set -euo pipefail
cd /Users/mushiquan/bili-annotator
export ANNOTATOR_HOST=0.0.0.0
export ANNOTATOR_PORT=5005
exec /usr/bin/caffeinate -s \
  /Users/mushiquan/.venvs/biliannotator/bin/python /Users/mushiquan/bili-annotator/app.py
