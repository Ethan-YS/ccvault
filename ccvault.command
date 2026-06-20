#!/bin/bash
# Double-click to launch ccvault (macOS). A Terminal window will stay open
# while it runs — just close it (or click Quit in the UI) to stop.
cd "$(dirname "$0")"
echo "Starting 📦 ccvault … your browser will open shortly."
echo "(Keep this window open while using it; close it to stop.)"
echo ""
python3 ccvault.py
