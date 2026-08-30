#!/usr/bin/env python3
"""Compatibility wrapper for the renamed source compiler.

Use ``compile_sources.py`` for new scripts and commands.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from scripts.compile_sources import compile_sources

compile_folder = compile_sources


if __name__ == "__main__":
    source_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if source_dir is None:
        raise SystemExit("用法: compile_folder.py SOURCE_DIR（建议改用 compile_sources.py）")
    asyncio.run(compile_sources(source_dir))
