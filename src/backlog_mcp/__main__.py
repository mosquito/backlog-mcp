"""`python -m backlog_mcp`, for a client that has no console script on its PATH."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
