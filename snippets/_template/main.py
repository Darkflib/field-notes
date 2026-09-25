# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""One-line description of what this demonstrates.

Run with: uv run main.py
Must exit 0 on success and must not prompt for input.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)


def main() -> int:
    """Demonstrate the thing. Return a process exit code."""
    log.info("hello")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())
