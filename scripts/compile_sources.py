"""Development entry point for the installed compile application service."""

from __future__ import annotations

import asyncio
import sys

from wiki_agent.application.compile_service import compile_sources

if __name__ == "__main__":
    asyncio.run(compile_sources(sys.argv[1] if len(sys.argv) > 1 else None))
