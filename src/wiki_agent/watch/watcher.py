"""生产端——事件驱动（inotify/watchdog）监视源目录，产出"变更待 ingest"的文件到队列。

事件管道:
    watchdog 事件 → 每路径去抖（settle）→ 稳定性复读 → 变更门 → queue.put

与轮询版的关系（两个入口共存，state 是共享真相源）:
- **事件路径**: 事件 → settle 窗口（吸收编辑器原子保存的 2-4 事件）
  → 稳定性复读（等价轮询版"两段确认"——同一内容隔 stability 仍
  不变才定案，防半写文件）→ 变更门（相似度 ≥ 阈值跳过微调）→ 入队
- **回退路径**: 每 fallback_interval 跑一次 _poll_once（全量扫描）。
  inotify 事件队列可能溢出/丢失，全量扫描是安全网；进程重启后的
  首次 reconcile 也走它。回退路径保留轮询版的两段确认（pending）。

两段确认语义迁移: 轮询版 = 同一内容连续 2 个轮询周期见到（~10s）；
事件版 = settle（事件静默 2s）+ 稳定性复读（再 2s 内容不变）。
保护对象相同（保存抖动/半写文件），确认时间缩短。

删除检测: 事件路径（文件消失时 settle 检查发现）+ 回退路径
（state 与磁盘 diff）双入口，都产出 ("delete", name) 队列项——
消费者契约不变。
"""

from __future__ import annotations

import asyncio
import hashlib
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING

from watchdog.events import FileSystemEventHandler
# Observer 是平台分发的运行时变量（非 class 定义）——类型注解见字段处
from watchdog.observers import Observer

if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver

from wiki_agent.log import get_logger
from wiki_agent.watch.state import FileState, WatchState

logger = get_logger("WATCHER")

# 回退路径的两段确认（轮询语义保留）
_CONFIRM_ROUNDS = 2
# 变更门: 相似度低于该值才算"大改动"（0-1）
_MIN_SIMILARITY = 0.7

# 事件路径时序（秒）
_SETTLE_WINDOW = 2.0      # 事件静默窗口——编辑器保存的原子操作在此内吸收
_STABILITY_DELAY = 2.0    # 稳定性复读间隔——第二次"看到同一内容"才定案
_FALLBACK_INTERVAL = 60.0  # 回退全量扫描周期——inotify 溢出/丢事件的安全网


class _FsEventHandler(FileSystemEventHandler):
    """watchdog 回调——只做最薄转发（观察者线程 → asyncio loop）。

    不在观察者线程做任何 IO/判定——编辑器保存的原子操作是
    src 消失 + dest 出现（MOVED），两个路径都要通知。
    """

    def __init__(self, watcher: "FileWatcher"):
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
    """事件驱动文件监视器——事件经去抖+稳定性确认后入队，回退扫描兜底。"""

    def __init__(
        self,
        source_dir: str | Path,
        queue: asyncio.Queue,
        state: WatchState,
        *,
        wiki_dir: str | Path | None = None,
        settle_window: float = _SETTLE_WINDOW,
        stability_delay: float = _STABILITY_DELAY,
        fallback_interval: float = _FALLBACK_INTERVAL,
        similarity_threshold: float = _MIN_SIMILARITY,
    ):
        self._root = Path(source_dir).resolve()
        self._queue = queue
        self._state = state
        # 源文件删除检测需要 wiki 目录（sources 页清理）——不传则只 drop state
        self._wiki_dir = Path(wiki_dir) if wiki_dir else None
        self._settle = settle_window
        self._stability = stability_delay
        self._fallback = fallback_interval
        self._threshold = similarity_threshold

        # 每路径去抖定时器——新事件重置旧定时器（编辑器多事件合并为一次检查）
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        # 注解用 BaseObserver（TYPE_CHECKING 导入）——Observer 在 watchdog 6
        # 是运行时变量赋值（平台分发），Pylance 禁止变量进类型表达式
        self._observer: "BaseObserver | None" = None

    # ── 事件桥（观察者线程侧）──────────────────────────────

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
        from wiki_agent.ingestion.data_loader import DataLoader
        return Path(path).suffix.lower() in DataLoader.ext_to_modality

    # ── 主循环 ─────────────────────────────────────────────

    async def run(self) -> None:
        """主循环——事件由 call_later 定时器自行驱动，这里只跑回退扫描。"""
        self._start_observer()
        logger.info(
            "watcher 启动（事件驱动）: %s（settle %.1fs, 稳定性 %.1fs, 回退扫描 %.0fs）",
            self._root, self._settle, self._stability, self._fallback,
        )
        try:
            while True:
                await asyncio.sleep(self._fallback)
                await self._poll_once()
        finally:
            self._stop_observer()

    def _start_observer(self) -> None:
        """启动 inotify 观察器。"""
        self._loop = asyncio.get_running_loop()
        self._observer = Observer()
        self._observer.schedule(
            _FsEventHandler(self), str(self._root), recursive=True)
        self._observer.start()
        logger.info("inotify 观察器已启动: %s", self._root)

    def _stop_observer(self) -> None:
        """停止观察器并清理定时器。"""
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None

    # ── 去抖与单路径检查（loop 侧）────────────────────────

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

    async def _check_path(self, path: str) -> list[str]:
        """settle 后检查单个路径——存在走变更门，不存在走删除。

        定时器句柄已在 _fire 里弹出，这里不重复 pop（_notify 的
        pop 语义是"取消旧定时器"——此处已无句柄）。

        Args:
            path: 文件路径。

        Returns:
            入队/删除的路径列表（事件路径用）。
        """
        p = Path(path)

        # ── 删除: 路径在 state 但磁盘上没了 ──
        if not p.exists():
            if path in self._state.all_paths():
                return await self._emit_delete(path)
            return []

        try:
            content1 = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        digest1 = hashlib.sha256(content1.encode("utf-8")).hexdigest()
        st = self._state.get(path)
        self._state.set(path, st)

        # 内容没变: touch/无意义写入 → 忽略
        if st.hash == digest1:
            return []

        # 变更门: 微调（相似度 ≥ 阈值）忽略
        if st.hash and not self._is_major_change(st, content1):
            logger.info("  %s: 相似度高于阈值，跳过（微调）", p.name)
            return []

        # 稳定性复读——两段确认的事件版：隔 stability 内容不变才定案
        await asyncio.sleep(self._stability)
        try:
            content2 = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []  # 复读时消失——删除路径由后续事件/回退扫描处理
        digest2 = hashlib.sha256(content2.encode("utf-8")).hexdigest()
        if digest2 != digest1:
            logger.debug("  %s: 稳定性窗口内又变化，重新进入 settle", p.name)
            self._notify(path)
            return []

        self._finalize_change(st, content2, digest2)
        self._state.save()
        await self._queue.put(p)
        logger.info("  变更入队: %s", p.name)
        return [str(p)]

    # ── 回退路径：全量扫描（轮询语义保留）──────────────────

    async def _poll_once(self) -> list[str]:
        """单轮全量扫描——inotify 溢出/丢事件的安全网 + 启动 reconcile。

        Returns:
            入队/删除的路径列表。
        """
        queued: list[str] = []
        current = self._scan_files()
        logger.debug("回退扫描: %d 个文件", len(current))

        for path in current:
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            st = self._state.get(str(path))
            # 新文件不在 entries 里时 get 返回临时对象——
            # 登记回去，否则 _confirm 对 pending 的修改在 save 时丢失
            self._state.set(str(path), st)

            if self._pass_change_gate(st, content, digest):
                await self._queue.put(path)
                queued.append(str(path))
                logger.info("  变更入队: %s", path.name)

        # 删除检测: state 里已不在磁盘上的文件 → 走共享 _emit_delete
        disk = {str(p) for p in current}
        for old in self._state.all_paths():
            if old not in disk:
                queued.extend(await self._emit_delete(old))

        self._state.save()
        return queued

    # ── 扫描 ──────────────────────────────────────────────

    def _scan_files(self) -> list[Path]:
        """扫描源目录下所有受支持扩展名的文件（递归）。

        Returns:
            文件路径列表。
        """
        from wiki_agent.ingestion.data_loader import DataLoader

        supported = DataLoader.ext_to_modality
        files: list[Path] = []
        for p in sorted(self._root.rglob("*")):
            if p.is_file() and p.suffix.lower() in supported:
                files.append(p)
        return files

    # ── 判定 ──────────────────────────────────────────────

    def _pass_change_gate(self, st: FileState, content: str, digest: str) -> bool:
        """两段确认 + 变更门（回退路径用）。

        返回 True → 入队（大改动确认）；False → 忽略或仅更新 pending。

        Args:
            st: 文件状态。
            content: 当前内容。
            digest: 当前内容哈希。

        Returns:
            True 表示应入队。
        """
        # 新文件: 无已知状态 → 走两段确认（首次见存 pending）
        if not st.hash:
            return self._confirm(st, content, digest)

        # 内容没变: touch/无意义写入 → 忽略
        if digest == st.hash:
            return False

        # hash 变了: 相似度门——微调忽略，大改动走确认
        if not self._is_major_change(st, content):
            return False
        return self._confirm(st, content, digest)

    async def _emit_delete(self, path: str) -> list[str]:
        """删除事件共享实现——drop state + ("delete", name) 入队。

        事件路径（单路径检查发现消失）与回退路径（state/磁盘 diff）
        都走这里——删除语义只有一处。

        Args:
            path: 被删文件路径。

        Returns:
            ["delete:<name>"] 标记列表。
        """
        name = Path(path).name
        self._state.drop(path)
        await self._queue.put(("delete", name))
        logger.info("  源文件删除检测: %s", name)
        return [f"delete:{name}"]

    def _is_major_change(self, st: FileState, content: str) -> bool:
        """与已知文本比相似度——低于阈值才算大改动。

        Args:
            st: 文件状态（已知文本）。
            content: 当前内容。

        Returns:
            True 表示大改动。
        """
        if st.text is None:
            return True
        if not st.text:
            return bool(content)
        return SequenceMatcher(None, st.text, content).ratio() < self._threshold

    def _confirm(self, st: FileState, content: str, digest: str) -> bool:
        """两段确认: 同一内容连续出现 _CONFIRM_ROUNDS 轮才通过。

        Args:
            st: 文件状态（pending 现场读写）。
            content: 当前内容。
            digest: 当前内容哈希。

        Returns:
            True 表示确认通过。
        """
        if st.pending_text is not None and st.pending_text == content:
            st.pending_seen += 1
            if st.pending_seen >= _CONFIRM_ROUNDS:
                self._finalize_change(st, content, digest)
                return True
            return False

        st.pending_text = content
        st.pending_seen = 1
        return False

    def _finalize_change(
            self, st: FileState, content: str, digest: str,
    ) -> None:
        """定案——把确认过的内容写进 state（两个入口共享）。

        事件路径（稳定性复读一致）与回退路径（两段确认通过）
        最终都走这里落 state——状态写入只有一处。

        Args:
            st: 文件状态（就地写入）。
            content: 定案内容。
            digest: 定案内容哈希。
        """
        st.hash = digest
        st.text = content
        st.pending_text = None
        st.pending_seen = 0
