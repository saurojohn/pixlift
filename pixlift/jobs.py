"""JobManager — 内存 job 字典 + asyncio.Lock + LRU/TTL 清理。

不持久化：进程重启 = job 全部丢失。V1 接受这个权衡（计划文档 §Non-Goals）。
"""

from __future__ import annotations

import asyncio
import secrets
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

# Job 状态机
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"done", "failed", "cancelled"}),
    "done": frozenset(),  # 终态
    "failed": frozenset(),  # 终态
    "cancelled": frozenset(),  # 终态
}

JobStatus = Literal["queued", "running", "done", "failed", "cancelled"]


class InvalidTransition(ValueError):
    """Job 状态机非法转换。"""


@dataclass
class Job:
    id: str
    status: JobStatus = "queued"
    progress: int = 0
    phase: str = ""  # 当前阶段：load_model / preprocess / infer / save
    model: str = ""
    scale: int = 4
    format: str = "png"
    input_dim: tuple[int, int] = (0, 0)
    output_dim: tuple[int, int] | None = None
    input_bytes: int = 0
    output_bytes: int | None = None
    work_dir: str = ""
    output_path: str | None = None
    started_at: float = 0.0
    finished_at: float | None = None
    error: str | None = None
    duration_s: float | None = None
    sync: bool = False  # 同步执行（小图）跳过 queue/polling

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _gen_job_id() -> str:
    """时间戳 + 8 hex 随机（4 byte = 2^32 种），极端并发也不撞。"""
    return f"j-{int(time.time())}-{secrets.token_hex(4)}"


class JobManager:
    """进程内单例 job 管理。"""

    def __init__(
        self,
        tmp_root: Path,
        max_count: int = 50,
        max_age_s: int = 600,
    ) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()
        self.tmp_root = tmp_root
        self.tmp_root.mkdir(parents=True, exist_ok=True)
        self.max_count = max_count
        self.max_age_s = max_age_s
        # in-flight asyncio.Task 跟踪（用于 cancel）
        self._tasks: dict[str, asyncio.Task] = {}

    async def create(
        self,
        model: str,
        scale: int,
        fmt: str,
        input_dim: tuple[int, int],
        input_bytes: int,
        sync: bool,
    ) -> Job:
        """创建新 job + 分配 work_dir。

        自动触发 cleanup()（LRU + TTL）。
        """
        async with self._lock:
            await self._cleanup_locked()
            # 极端并发（同一毫秒同 batch 内）也保证 unique
            for _ in range(10):
                jid = _gen_job_id()
                if jid not in self._jobs:
                    break
            else:
                raise RuntimeError("Failed to generate unique job_id after 10 attempts")
            work_dir = self.tmp_root / jid
            work_dir.mkdir(parents=True, exist_ok=True)
            job = Job(
                id=jid,
                model=model,
                scale=scale,
                format=fmt,
                input_dim=input_dim,
                input_bytes=input_bytes,
                work_dir=str(work_dir),
                started_at=time.time(),
                sync=sync,
            )
            self._jobs[jid] = job
            return job

    async def get(self, job_id: str) -> Job | None:
        async with self._lock:
            return self._jobs.get(job_id)

    async def update(self, job_id: str, **fields) -> Job:
        """原子更新字段，校验状态机。

        允许的字段：status, progress, phase, error, output_path, output_dim,
        output_bytes, finished_at, duration_s。
        """
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"Job not found: {job_id}")

            if "status" in fields:
                new_status = fields["status"]
                allowed = VALID_TRANSITIONS[job.status]
                if new_status not in allowed and new_status != job.status:
                    raise InvalidTransition(
                        f"Cannot transition {job.status} → {new_status}"
                    )

            for k, v in fields.items():
                if not hasattr(job, k):
                    raise AttributeError(f"Job has no field: {k}")
                setattr(job, k, v)
            return job

    def register_task(self, job_id: str, task: asyncio.Task) -> None:
        """注册 in-flight 任务，便于 cancel。"""
        self._tasks[job_id] = task

    def unregister_task(self, job_id: str) -> None:
        self._tasks.pop(job_id, None)

    async def cancel(self, job_id: str) -> bool:
        """取消 in-flight 任务 + 标记 cancelled。返回是否成功。"""
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status in ("done", "failed", "cancelled"):
                return True  # 已经是终态，幂等成功
            # 1. 取消 asyncio.Task（cancel 在 worker thread 上调无效，但 Engine
            #    的 worker 是 asyncio.to_thread 包装的，cancel 触发 CancelledError
            #    会让 to_thread 抛 — 不依赖 worker 立即停）
            task = self._tasks.get(job_id)
            if task is not None and not task.done():
                task.cancel()
            # 2. 标记 cancelled（cancelled 是终态）
            job.status = "cancelled"
            job.finished_at = time.time()
            job.error = "cancelled by user"
            return True

    async def list_recent(self, n: int = 20) -> list[dict]:
        async with self._lock:
            jobs = sorted(
                self._jobs.values(),
                key=lambda j: j.started_at,
                reverse=True,
            )[:n]
            return [j.to_dict() for j in jobs]

    async def cleanup(self) -> int:
        """LRU + TTL 清理，返回删除的 job 数。"""
        async with self._lock:
            return await self._cleanup_locked()

    async def _cleanup_locked(self) -> int:
        """LRU + TTL 清理，返回删除的 job 总数（TTL + LRU）。"""
        now = time.time()
        # 1. TTL：finished_at > max_age_s
        stale = [
            jid
            for jid, j in self._jobs.items()
            if j.finished_at is not None and now - j.finished_at > self.max_age_s
        ]
        for jid in stale:
            self._delete_locked(jid)

        # 2. LRU：超过 max_count 时淘汰最早的 finished
        evicted = 0
        if len(self._jobs) > self.max_count:
            finished = sorted(
                (
                    (jid, j)
                    for jid, j in self._jobs.items()
                    if j.finished_at is not None
                ),
                key=lambda kv: kv[1].finished_at or 0,
            )
            excess = len(self._jobs) - self.max_count
            for jid, _ in finished[:excess]:
                self._delete_locked(jid)
                evicted += 1

        return len(stale) + evicted

    def _delete_locked(self, job_id: str) -> None:
        """删除 job + 清理 tmp 目录（必须在持有 lock 时调用）。"""
        job = self._jobs.pop(job_id, None)
        if job is None:
            return
        p = Path(job.work_dir)
        if p.exists() and p.is_relative_to(self.tmp_root):
            try:
                shutil.rmtree(p)
            except OSError as e:
                import logging

                logging.getLogger(__name__).warning(
                    "rmtree failed for %s: %s — leaking tmp dir", p, e
                )

    def cleanup_stale_tmp(self) -> int:
        """启动时调用：清理 tmp/ 中非 JobManager 拥有的孤儿目录。"""
        removed = 0
        if not self.tmp_root.exists():
            return 0
        known = {j.id for j in self._jobs.values()}
        import logging

        log = logging.getLogger(__name__)
        for child in self.tmp_root.iterdir():
            if child.is_dir() and child.name not in known:
                try:
                    shutil.rmtree(child)
                    removed += 1
                except OSError as e:
                    log.warning("rmtree stale tmp %s: %s", child, e)
        return removed

    def __len__(self) -> int:
        return len(self._jobs)
