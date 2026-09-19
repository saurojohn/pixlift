"""JobManager 测试。"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from pixlift.jobs import InvalidTransition, Job, JobManager


@pytest.fixture
def jm(tmp_path):
    return JobManager(tmp_root=tmp_path / "tmp", max_count=3, max_age_s=60)


async def test_create_job(jm: JobManager):
    j = await jm.create(
        model="realesrgan-x4plus",
        scale=4,
        fmt="png",
        input_dim=(100, 200),
        input_bytes=1234,
        sync=False,
    )
    assert j.id.startswith("j-")
    assert j.status == "queued"
    assert Path(j.work_dir).exists()


async def test_state_machine_valid_transitions(jm: JobManager):
    j = await jm.create(
        model="realesrgan-x4plus", scale=4, fmt="png",
        input_dim=(10, 10), input_bytes=1, sync=False,
    )
    await jm.update(j.id, status="running")
    assert (await jm.get(j.id)).status == "running"
    await jm.update(j.id, status="done", finished_at=time.time())
    assert (await jm.get(j.id)).status == "done"


async def test_state_machine_invalid_skip_to_queued(jm: JobManager):
    """running → queued 是非法跳过（必须走 done/failed）。"""
    j = await jm.create(
        model="realesrgan-x4plus", scale=4, fmt="png",
        input_dim=(10, 10), input_bytes=1, sync=False,
    )
    await jm.update(j.id, status="running")
    with pytest.raises(InvalidTransition):
        await jm.update(j.id, status="queued")


async def test_state_machine_terminal_done_no_more_changes(jm: JobManager):
    j = await jm.create(
        model="realesrgan-x4plus", scale=4, fmt="png",
        input_dim=(10, 10), input_bytes=1, sync=False,
    )
    await jm.update(j.id, status="running")
    await jm.update(j.id, status="done")
    with pytest.raises(InvalidTransition):
        await jm.update(j.id, status="running")


async def test_lru_eviction(jm: JobManager):
    """max_count=3 → 第 4 个 finished job 被 LRU 淘汰。"""
    jobs = []
    for i in range(4):
        j = await jm.create(
            model="realesrgan-x4plus", scale=4, fmt="png",
            input_dim=(10, 10), input_bytes=1, sync=False,
        )
        await jm.update(j.id, status="running")
        await jm.update(j.id, status="done", finished_at=time.time() + i * 0.01)
        jobs.append(j)
        # 等触发 cleanup
        await jm.cleanup()
    # 最后创建的应保留，最早的应被淘汰
    assert (await jm.get(jobs[-1].id)) is not None
    assert (await jm.get(jobs[0].id)) is None
    # 最早 job 的 tmp 目录应已被删除
    assert not Path(jobs[0].work_dir).exists()


async def test_ttl_cleanup(jm: JobManager):
    """max_age_s 太小 → 立即清理 finished jobs。"""
    jm.max_age_s = 0  # 立即过期
    j = await jm.create(
        model="realesrgan-x4plus", scale=4, fmt="png",
        input_dim=(10, 10), input_bytes=1, sync=False,
    )
    await jm.update(j.id, status="running")
    await jm.update(j.id, status="done", finished_at=time.time() - 1)
    removed = await jm.cleanup()
    assert removed == 1
    assert (await jm.get(j.id)) is None
    assert not Path(j.work_dir).exists()


async def test_concurrent_create_no_race(jm: JobManager):
    """100 个并发 create_job 应全部有唯一 id。"""
    coros = [
        jm.create(
            model="realesrgan-x4plus", scale=4, fmt="png",
            input_dim=(10, 10), input_bytes=1, sync=False,
        )
        for _ in range(100)
    ]
    jobs = await asyncio.gather(*coros)
    assert len({j.id for j in jobs}) == 100


async def test_update_unknown_job_raises(jm: JobManager):
    with pytest.raises(KeyError):
        await jm.update("j-doesnt-exist", status="running")


async def test_update_unknown_field_raises(jm: JobManager):
    j = await jm.create(
        model="realesrgan-x4plus", scale=4, fmt="png",
        input_dim=(10, 10), input_bytes=1, sync=False,
    )
    with pytest.raises(AttributeError):
        await jm.update(j.id, not_a_field="x")


async def test_list_recent_order(jm: JobManager):
    js: list[Job] = []
    for i in range(3):
        j = await jm.create(
            model="realesrgan-x4plus", scale=4, fmt="png",
            input_dim=(10, 10), input_bytes=1, sync=False,
        )
        js.append(j)
        await asyncio.sleep(0.01)
    listed = await jm.list_recent(n=10)
    # 最新在前
    assert listed[0]["id"] == js[-1].id


async def test_cleanup_stale_orphan_dirs(tmp_path):
    """启动时清理孤儿目录。"""
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    # 不在 jm 中但存在的目录
    (tmp / "j-orphan-1").mkdir()
    (tmp / "j-orphan-2").mkdir()
    jm = JobManager(tmp_root=tmp, max_count=10, max_age_s=60)
    removed = jm.cleanup_stale_tmp()
    assert removed == 2
    assert list(tmp.iterdir()) == []