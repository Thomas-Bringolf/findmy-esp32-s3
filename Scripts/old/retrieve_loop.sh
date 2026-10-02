#!/bin/bash
# Background Apple report retrieval loop.
# Runs retrieve_rotating.py repeatedly (new session each attempt so a
# stale/expired Apple session cannot wedge the loop), logging to
# state/retrieve_loop.log. Started with `make retrieve-loop` or by hand.
set -u
cd "$(dirname "$0")"
LOG=state/retrieve_loop.log
echo "=== retrieval loop started $(date -Is) ===" >> "$LOG"
while true; do
    timeout 120 python3 -u retrieve_rotating.py >> "$LOG" 2>&1
    echo "--- attempt finished $(date -Is) ---" >> "$LOG"
    sleep 90
done
