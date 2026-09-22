"""Load the application's SQL definitions once."""

from pathlib import Path

import aiosql

QUERIES = aiosql.from_path(Path(__file__).with_suffix(".sql"), "aiosqlite")
