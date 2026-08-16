"""全链条测试——清空 → 全量生成 → refine → 手术（dry-run）。

用法:
    VIRTUAL_ENV= .venv/bin/python scripts/full_pipeline.py <源目录> [--keep-wiki]

阶段:
    0. 清空 wiki（保留 purpose/schema）——除非 --keep-wiki
    1. compile: 全量生成（scripts/compile_folder.py 的 main）
    2. refine:  wiki 自编译（scripts/refine_wiki.py 的 main）
    3. surgery: 结构手术 dry-run（只出报告不动手）

每阶段一个 run 容器（runs/<stage>_<ts>/），阶段失败不中断后续？
不——失败中断并给出 trace 位置。全量测试要的是每个阶段都真实跑完，
中间失败继续跑后面的会污染下一阶段的输入。
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from wiki_agent.log import get_logger  # noqa: E402

logger = get_logger("FULL_PIPELINE")


async def stage_compile(source_dir: str) -> None:
    """阶段 1: 全量生成。"""
    from compile_folder import main as compile_main
    await compile_main(source_dir)


async def stage_refine() -> None:
    """阶段 2: wiki 自编译。"""
    from refine_wiki import main as refine_main
    await refine_main()


async def stage_surgery() -> None:
    """阶段 3: 结构手术 dry-run（只出报告不动手）。"""
    from surgery_wiki import main as surgery_main
    await surgery_main(dry_run=True)


def clear_wiki() -> None:
    """阶段 0: 清空 wiki 内容（保留 purpose/schema）。"""
    wiki = PROJECT_ROOT / "wiki"
    for sub in ("concepts", "entities", "sources", "topics"):
        p = wiki / sub
        if p.exists():
            import shutil
            shutil.rmtree(p)
    index = wiki / "index.md"
    if index.exists():
        index.unlink()
    print(f"[0/3] wiki 已清空（保留 purpose.md/schema.md）")


async def main():
    if len(sys.argv) < 2 or sys.argv[1].startswith("--"):
        print(f"用法: {sys.argv[0]} <源目录路径> [--keep-wiki]")
        sys.exit(1)
    source_dir = sys.argv[1]
    keep = "--keep-wiki" in sys.argv
    if not keep:
        clear_wiki()

    print("\n════════ [1/3] compile 全量生成 ════════")
    await stage_compile(source_dir)
    print("✅ [1/3] compile 完成")

    print("\n════════ [2/3] refine 自编译 ════════")
    await stage_refine()
    print("✅ [2/3] refine 完成")

    print("\n════════ [3/3] surgery 结构手术 dry-run ════════")
    await stage_surgery()
    print("✅ [3/3] surgery dry-run 完成")

    print("\n全链条完成。追溯: wiki/.logs/runs/ 下按时间戳的三个 run 容器。")


if __name__ == "__main__":
    asyncio.run(main())
