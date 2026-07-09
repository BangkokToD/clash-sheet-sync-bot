"""Compatibility launcher for running the bot as `python bot.py`."""

from __future__ import annotations

from clash_sheet_sync_bot.bot import main

if __name__ == "__main__":
    raise SystemExit(main())
