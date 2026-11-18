#!/usr/bin/env bash
# Run the bot in the foreground (for a quick test on the VPS). Ctrl+C to stop.
cd "$(dirname "$0")/.." && exec python3 -u bot.py
