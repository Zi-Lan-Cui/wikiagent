"""Development launcher for manifest-driven batch compilation."""

import asyncio

from wiki_agent.application.batch_compile import _main, _parser

if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(_parser().parse_args())))
