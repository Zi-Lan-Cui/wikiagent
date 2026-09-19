"""生产端——事件驱动监视源目录，把"变更待 ingest"的文件提交为 Job。

事件管道: 变更事件 → 去抖（吸收编辑器保存抖动）→ 稳定性复读
（同一内容隔窗口不变才定案，防半写文件）→ 变更门（相似度过阈值的
微调跳过）→ submit_job(绝对路径, deleted, digest)。

纯生产者：本模块只提交意图（digest = 确认时读到的内容指纹），**不写
"已处理"账**。state.hash/text 只在 job 成功后由核账入口写入（I4）——
提交未确认期间，后续扫描对同内容的重复提交由在途 Job 的唯一索引幂等
吸收（I1），确定性失败让位于 issue 重试通道（I6）。

另有周期性全量扫描兜底: 事件可能溢出/丢失，全量扫描是安全网，
进程重启后的首次 reconcile 也走它。删除检测由事件与回退两条路径共同
覆盖；state 条目延迟到 delete job 成功后才清除（Job 失败可被重新发现）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING

from watchdog.events import FileSystemEventHandler

# Observer 是平台分发的运行时变量（非 class 定义）——类型注解见字段处
from watchdog.observers import Observer

if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver

from wiki_agent.log import get_logger
from wiki_agent.watch.state import FileState, WatchState, digest_file_text

logger = get_logger("WATCHER")

# 回退路径的两段确认（轮询语义保留）
_CONFIRM_ROUNDS = 2
# 变更门: 相似度低于该值才算"大改动"（0-1）
_MIN_SIMILARITY = 0.7

# 事件路径时序（秒）
_SETTLE_WINDOW = 2.0  # 事件静默窗口——编辑器保存的原子操作在此内吸收
_STABILITY_DELAY = 2.0  # 稳定性复读间隔——第二次"看到同一内容"才定案
_FALLBACK_INTERVAL = 60.0  # 回退全量扫描周期——事件溢出/丢事件的安全网


class _FsEventHandler(FileSystemEventHandler):
    """watchdog 回调——只做最薄转发（观察者线程 → asyncio loop）。

    不在观察者线程做任何 IO/判定——编辑器保存的原子操作是
    src 消失 + dest 出现（MOVED），两个路径都要通知。
    """

    def __init__(self, watcher: FileWatcher):
        self._watcher = watcher

    def on_any_event(self, event):
        if event.is_directory:
            return
        src = getattr(event, "src_path", None)
        dest = getattr(event, "dest_path", None)
        for path in (src, dest):
            if path and self._watcher._is_supported(path):
                self._watcher._bridge_event(str(path))


class FileWatcher:
    """事件驱动文件监视器——确认后的变更提交为 Job，回退扫描兜底。"""

    def __init__(
        self,
        source_dir: str | Path,
        state: WatchState,
        *,
        wiki_dir: str | Path | None = None,
        settle_window: float = _SETTLE_WINDOW,
        stability_delay: float = _STABILITY_DELAY,
        fallback_interval: float = _FALLBACK_INTERVAL,
        similarity_threshold: float = _MIN_SIMILARITY,
        submit_job: Callable[[str, bool, str], object],
    ):
        self._root = Path(source_dir).resolve()
        self._state = state
        # 源文件删除检测需要 Wiki 目录（清理旧式来源引用）——不传则只 drop state
        self._wiki_dir = Path(wiki_dir) if wiki_dir else None
        self._settle = settle_window
        self._stability = stability_delay
        self._fallback = fallback_interval
        self._threshold = similarity_threshold
        # (resource, deleted, digest) → Job——提交是唯一出口；resource 一律
        # 绝对路径字符串（与 issue 重试链共享 I1 身份空间）
        self._submit_job = submit_job

        # 每路径去抖定时器——新事件重置旧定时器（编辑器多事件合并为一次检查）
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        # 注解用 BaseObserver（TYPE_CHECKING 导入）——Observer 在 watchdog 6
        # 是运行时变量赋值（平台分发），Pylance 禁止变量进类型表达式
        self._observer: BaseObserver | None = None

    # 事件桥（观察者线程侧）

    def _bridge_event(self, path: str) -> None:
        """线程安全转发到 loop——不阻塞观察者线程。

        Args:
            path: 触发事件的文件路径。
        """
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._notify, path)
        except RuntimeError:
            pass  # loop 已关闭（shutdown 竞态）

    def _is_supported(self, path: str) -> bool:
        """受支持扩展名——与 ingest 接受范围一致（复用 DataLoader 模态表）。

        Args:
            path: 文件路径。

        Returns:
            True 表示扩展名受支持。
        """
        from wiki_agent.documents.loader import DataLoader

        return Path(path).suffix.lower() in DataLoader.ext_to_modality

    # 主循环

    async def run(self) -> None:
        """主循环——事件由 call_later 定时器自行驱动，这里只跑回退扫描。"""
        self._start_observer()
        logger.info(
            "watcher 启动（事件驱动）: %s（settle %.1fs, 稳定性 %.1fs, 回退扫描 %.0fs）",
            self._root,
            self._settle,
            self._stability,
            self._fallback,
        )
        try:
            while True:
                await asyncio.sleep(self._fallback)
                await self._poll_once()
        finally:
            self._stop_observer()

    def _start_observer(self) -> None:
        """启动文件事件观察器。"""
        self._loop = asyncio.get_running_loop()
        self._observer = Observer()
        self._observer.schedule(_FsEventHandler(self), str(self._root), recursive=True)
        self._observer.start()
        logger.info("文件观察器已启动: %s", self._root)

    def _stop_observer(self) -> None:
        """停止观察器并清理定时器。"""
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None

    # 去抖与单路径检查（loop 侧）

    def _notify(self, path: str) -> None:
        """重置该路径的 settle 定时器——新事件重新计时（去抖核心）。

        _check_path 是 async 函数——call_later 只接受普通回调，
        包一层同步壳在回调里 create_task 调度协程。

        Args:
            path: 触发事件的文件路径。
        """
        if self._loop is None:
            return
        old = self._timers.pop(path, None)
        if old is not None:
            old.cancel()

        def _fire() -> None:
            self._timers.pop(path, None)
            asyncio.create_task(self._check_path(path))

        self._timers[path] = self._loop.call_later(self._settle, _fire)

    async def _check_path(self, path: str) -> None:
        """settle 后检查单个路径——存在走变更门，不存在走删除。

        稳定性窗口可能跨越一次回退扫描，sleep 之后必须重取 st 再变更
        （否则会把扫描期间别的写入冲掉）。

        Args:
            path: 文件路径。
        """
        p = Path(path)

        # 删除: 路径在 state 但磁盘上没了
        if not p.exists():
            if path in self._state.all_paths():
                await self._emit_delete(path)
            return

        read = digest_file_text(p)
        if read is None:
            return
        digest1, text1 = read
        st = self._state.get(path)
        self._state.set(path, st)

        # 内容没变: touch/无意义写入 → 忽略
        if st.hash == digest1:
            return

        # 变更门: 微调（相似度 ≥ 阈值）忽略
        if st.hash and not self._is_major_change(st, text1):
            logger.info("  %s: 相似度高于阈值，跳过（微调）", p.name)
            return

        # 稳定性复读——两段确认的事件版：隔 stability 内容不变才定案
        await asyncio.sleep(self._stability)
        read2 = digest_file_text(p)
        if read2 is None:
            return  # 复读时消失——删除路径由后续事件/回退扫描处理
        if read2[0] != digest1:
            logger.debug("  %s: 稳定性窗口内又变化，重新进入 settle", p.name)
            self._notify(path)
            return

        st = self._state.get(path)
        self._clear_pending(st)
        self._state.set(path, st)
        self._state.save()
        self._submit_job(str(p), False, digest1)
        logger.info("  变更提交: %s", p.name)

    # 回退路径：全量扫描（轮询语义保留）

    async def _poll_once(self) -> None:
        """单轮全量扫描——事件溢出/丢失的安全网 + 启动 reconcile。"""
        current = self._scan_files()
        logger.debug("回退扫描: %d 个文件", len(current))

        for path in current:
            read = digest_file_text(path)
            if read is None:
                continue
            digest, text = read
            st = self._state.get(str(path))
            # 新文件不在 entries 里时 get 返回临时对象——
            # 登记回去，否则 _confirm 对 pending 的修改在 save 时丢失
            self._state.set(str(path), st)

            if self._pass_change_gate(st, text, digest):
                self._submit_job(str(path), False, digest)
                logger.info("  变更提交: %s", path.name)

        # 删除检测: state 里已不在磁盘上的文件 → 共享 _emit_delete。
        # 条目在 delete job 成功前保留——它是"这件事还没做完"的持久凭证，
        # 每轮重提交被在途 Job 幂等吸收；崩溃/失败后重启可重新发现。
        disk = {str(p) for p in current}
        for old in self._state.all_paths():
            if old not in disk:
                await self._emit_delete(old)

        self._state.save()

    # 扫描

    def _scan_files(self) -> list[Path]:
        """扫描源目录下所有受支持扩展名的文件（递归）。

        Returns:
            文件路径列表。
        """
        from wiki_agent.documents.loader import DataLoader

        supported = DataLoader.ext_to_modality
        files: list[Path] = []
        for p in sorted(self._root.rglob("*")):
            if p.is_file() and p.suffix.lower() in supported:
                files.append(p)
        return files

    # 判定

    def _pass_change_gate(self, st: FileState, text: str, digest: str) -> bool:
        """两段确认 + 变更门（回退路径用）。

        返回 True → 提交 Job（大改动确认完成）；False → 忽略或仅更新 pending。

        Args:
            st: 文件状态。
            text: 当前内容。
            digest: 当前内容哈希。

        Returns:
            True 表示应提交。
        """
        # 新文件: 无已知状态 → 走两段确认（首次见存 pending）
        if not st.hash:
            return self._confirm(st, text, digest)

        # 内容没变: touch/无意义写入 → 忽略
        if digest == st.hash:
            return False

        # hash 变了: 相似度门——微调忽略，大改动走确认
        if not self._is_major_change(st, text):
            return False
        return self._confirm(st, text, digest)

    async def _emit_delete(self, path: str) -> None:
        """删除事件共享实现——提交 delete Job（资源 = 绝对路径）。

        事件路径（单路径检查发现消失）与回退路径（state/磁盘 diff）
        都走这里——删除语义只有一处。state 条目不在此清除：成功账由
        JobOutcomeHandler 在 delete job 成功后落（延迟 drop，可自愈）。

        Args:
            path: 被删文件路径。
        """
        self._submit_job(str(Path(path).resolve()), True, "")
        logger.info("  源文件删除提交: %s", Path(path).name)

    def _is_major_change(self, st: FileState, text: str) -> bool:
        """与已知内容比相似度——低于阈值才算大改动。

        Args:
            st: 文件状态（已知文本）。
            text: 当前内容。

        Returns:
            True 表示大改动。
        """
        if st.text is None:
            return True
        if not st.text:
            return bool(text)
        return SequenceMatcher(None, st.text, text).ratio() < self._threshold

    def _confirm(self, st: FileState, text: str, digest: str) -> bool:
        """两段确认: 同一内容连续出现 _CONFIRM_ROUNDS 轮才通过。

        Args:
            st: 文件状态（pending 现场读写）。
            text: 当前内容。
            digest: 当前内容哈希。

        Returns:
            True 表示确认通过。
        """
        if st.pending_text is not None and st.pending_text == text:
            st.pending_seen += 1
            if st.pending_seen >= _CONFIRM_ROUNDS:
                self._clear_pending(st)
                return True
            return False

        st.pending_text = text
        st.pending_seen = 1
        return False

    def _clear_pending(self, st: FileState) -> None:
        """确认定案——清两段确认现场。

        只清 pending，不写 hash/text：完成账本唯一写入口是
        WatchState.record（job 成功后由 outcome 落），这是 I4 的
        生产侧表达。
        """
        st.pending_text = None
        st.pending_seen = 0
