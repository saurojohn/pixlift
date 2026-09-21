"""PixLift FastAPI 应用。

路由：
- GET  /                       → 前端 index.html
- GET  /static/{path}          → 静态资源
- GET  /api/health             → 健康 + binary/Vulkan 信息
- POST /api/upscale            → 单张 multipart（<2MB 同步 / ≥2MB 异步）
- POST /api/upscale/batch      → 多张 multipart（全部异步，串行处理）
- GET  /api/jobs/{id}          → job 元数据 + 进度
- GET  /api/jobs/{id}/download → 输出文件
- GET  /api/batches/{id}       → batch 进度 + job_id 列表
- GET  /api/batches/{id}/zip   → 全部 done 的输出打包 ZIP
"""

from __future__ import annotations

import asyncio
import logging
import time
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile  # noqa: B008
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from pixlift import STATIC_DIR, __version__
from pixlift.config import Settings, load_settings
from pixlift.engine import (
    SUPPORTED_LONG_EDGES,
    SUPPORTED_MODELS,
    SUPPORTED_SCALES,
    Engine,
    EngineError,
    resolve_scale,
)
from pixlift.jobs import JobManager
from pixlift.validators import (
    content_type_for,
    validate_image_bytes,
    validate_out_format,
)

log = logging.getLogger("pixlift")


@dataclass
class Batch:
    """多张批量处理。"""

    id: str
    job_ids: list[str] = field(default_factory=list)
    created_at: float = 0.0
    total: int = 0


class BatchManager:
    """简单内存 batch 管理器（与 JobManager 配套）。"""

    def __init__(self, max_count: int = 30) -> None:
        self._batches: dict[str, Batch] = {}
        self._lock = asyncio.Lock()
        self.max_count = max_count

    async def create(self, total: int) -> Batch:
        import secrets

        async with self._lock:
            bid = f"b-{int(time.time())}-{secrets.token_hex(2)}"
            b = Batch(id=bid, total=total, created_at=time.time())
            self._batches[bid] = b
            # LRU：超过上限删最早
            if len(self._batches) > self.max_count:
                oldest = min(self._batches.values(), key=lambda x: x.created_at)
                self._batches.pop(oldest.id, None)
            return b

    async def add_job(self, batch_id: str, job_id: str) -> None:
        async with self._lock:
            b = self._batches.get(batch_id)
            if b is not None:
                b.job_ids.append(job_id)

    async def get(self, batch_id: str) -> Batch | None:
        async with self._lock:
            return self._batches.get(batch_id)


def _make_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    # PyTorch 后端：不需要 binary，直接 init（不需要 binary path）
    try:
        engine = Engine.from_env(
            binary_path=None,
            model_dir=settings.model_dir,
            timeout_s=settings.job_timeout_s,
            extra_args=settings.extra_args,
        )
        engine.set_max_concurrent(settings.max_concurrent_upscales)
    except EngineError as e:
        log.warning("engine init failed: %s", e)
        engine = None

    job_mgr = JobManager(
        tmp_root=settings.tmp_root,
        max_count=settings.max_jobs,
        max_age_s=600
        if settings.keep_tmp_hours == 0
        else settings.keep_tmp_hours * 3600,
    )
    job_mgr.cleanup_stale_tmp()
    batch_mgr = BatchManager(max_count=settings.max_batches)

    # 持有异步任务引用，防止 GC 回收导致处理中断
    _active_tasks: set[asyncio.Task] = set()

    def _track_task(coro, job_id: str | None = None) -> asyncio.Task:
        task = asyncio.create_task(coro)
        _active_tasks.add(task)
        if job_id is not None:
            job_mgr.register_task(job_id, task)
            task.add_done_callback(lambda t: job_mgr.unregister_task(job_id))
        task.add_done_callback(_active_tasks.discard)
        return task

    cleanup_task: asyncio.Task | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal cleanup_task
        log.info(
            "pixlift %s | backend=python-realesrgan | tmp=%s | model_dir=%s",
            __version__,
            settings.tmp_root,
            settings.model_dir,
        )
        cleanup_task = asyncio.create_task(_periodic_cleanup(job_mgr))
        try:
            yield
        finally:
            if cleanup_task is not None:
                cleanup_task.cancel()
            # 等所有 active job 任务结束（带超时，避免挂死）
            if _active_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*_active_tasks, return_exceptions=True),
                        timeout=5,
                    )
                except asyncio.TimeoutError:
                    log.warning(
                        "%d active tasks did not finish in 5s", len(_active_tasks)
                    )
                    for t in _active_tasks:
                        t.cancel()
            if cleanup_task is not None:
                try:
                    await cleanup_task
                except asyncio.CancelledError:
                    pass  # 正常取消
                except Exception:
                    log.exception("cleanup task raised on shutdown")

    app = FastAPI(
        title="PixLift",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
    )

    # ---- CORS（仅当显式配置） ----
    if settings.cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # ---- 静态资源（开发期禁缓存，避免改 JS 不生效）----
    from starlette.staticfiles import StaticFiles as _SF

    class NoCacheStatic(_SF):
        async def get_response(self, path, scope):
            resp = await super().get_response(path, scope)
            if isinstance(resp, _SF):  # 目录列表（开发期不该发生）
                return resp
            # FileResponse：加 no-cache
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            return resp

    app.mount("/static", NoCacheStatic(directory=str(STATIC_DIR)), name="static")

    # ---- helpers ----

    def _parse_scale_or_longedge(
        scale_raw: str | None,
        long_edge_raw: str | None,
        meta_width: int,
        meta_height: int,
    ) -> int:
        """根据 scale 或 long_edge 参数，返回实际倍率（2/3/4）。

        long_edge 优先级 > scale。
        校验：long_edge 必须是白名单值（否则 422）；
        scale 必须是 2/3/4（否则 422）。
        """
        long_edge_white = {str(x) for x in SUPPORTED_LONG_EDGES}

        if long_edge_raw:
            if long_edge_raw not in long_edge_white:
                raise HTTPException(
                    422,
                    f"Unsupported long_edge: {long_edge_raw}. Allowed: {sorted(SUPPORTED_LONG_EDGES)}",
                )
            return resolve_scale(int(long_edge_raw), max(meta_width, meta_height))

        if not scale_raw:
            return 4  # 默认
        try:
            s = int(scale_raw)
        except (TypeError, ValueError):
            raise HTTPException(422, f"Invalid scale: {scale_raw}")
        if s not in SUPPORTED_SCALES:
            raise HTTPException(
                422, f"Unsupported scale: {s}. Allowed: {list(SUPPORTED_SCALES)}"
            )
        return s

    def _parse_quality(quality_raw: str | None, fmt: str) -> int | None:
        """quality 仅 jpg/webp 有效；png 强制 None。"""
        if fmt == "png":
            return None
        if quality_raw in (None, ""):
            return 90  # 默认高质量
        try:
            q = int(quality_raw)
        except (TypeError, ValueError):
            raise HTTPException(422, f"Invalid quality: {quality_raw}")
        if not (1 <= q <= 100):
            raise HTTPException(422, f"quality must be 1..100, got {q}")
        return q

    # ---- 路由 ----

    @app.get("/", response_class=HTMLResponse)
    async def root():
        idx = STATIC_DIR / "index.html"
        if not idx.exists():
            raise HTTPException(404, "Frontend not built (index.html missing)")
        return HTMLResponse(idx.read_text(encoding="utf-8"))

    @app.get("/api/health")
    async def health():
        if engine is None:
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "error": "engine_not_initialized",
                    "message": "Engine not initialized. Check PyTorch + model files.",
                    "version": __version__,
                    "supported_long_edges": list(SUPPORTED_LONG_EDGES),
                },
            )
        info = await engine.health_check()
        info["ok"] = bool(info.get("ready"))
        info["version"] = __version__
        info["supported_models"] = sorted(SUPPORTED_MODELS)
        info["supported_scales"] = list(SUPPORTED_SCALES)
        info["supported_long_edges"] = list(SUPPORTED_LONG_EDGES)
        return JSONResponse(status_code=200 if info["ok"] else 503, content=info)

    @app.post("/api/upscale")
    async def upscale(
        image: UploadFile = File(...),
        model: str = Form(...),
        scale: str = Form("4"),
        long_edge: str = Form(""),
        format: str = Form("png"),
        quality: str = Form(""),
    ):
        # 参数校验
        if model not in SUPPORTED_MODELS:
            raise HTTPException(
                422, f"Unsupported model. Allowed: {sorted(SUPPORTED_MODELS)}"
            )
        out_fmt = validate_out_format(format)

        # 读取（限 max_upload_mb）
        max_bytes = settings.max_upload_mb * 1024 * 1024
        data = await image.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise HTTPException(
                413, f"Upload too large. Max {settings.max_upload_mb} MB."
            )

        meta = validate_image_bytes(
            data,
            max_bytes=max_bytes,
            max_long_edge=settings.max_long_edge,
        )

        # scale/long_edge → 实际倍率
        actual_scale = _parse_scale_or_longedge(
            scale, long_edge, meta.width, meta.height
        )
        actual_quality = _parse_quality(quality, out_fmt)

        # 参数 + 输入都合法后才检查 engine 可用性
        if engine is None:
            raise HTTPException(
                503,
                "Engine not initialized (binary missing). Check /api/health.",
            )

        # 决定 sync vs async：sync 现在只标记 job.sync 字段用于统计；
        # 两个分支都返回 202 + job_id（前端统一走 poll → download 流程），
        # 这样小图（sync）也能看到分阶段进度（5%→40%→85%→95%→100%）。
        is_sync = len(data) < settings.sync_threshold_bytes

        job = await job_mgr.create(
            model=model,
            scale=actual_scale,
            fmt=out_fmt,
            input_dim=(meta.width, meta.height),
            input_bytes=len(data),
            sync=is_sync,
        )

        ext = meta.format if meta.format != "jpeg" else "jpg"
        input_path = Path(job.work_dir) / f"input.{ext}"
        output_path = Path(job.work_dir) / f"output.{out_fmt}"
        await asyncio.to_thread(input_path.write_bytes, data)

        # sync 和 async 都用 background task，让 _run_job 阶段化进度能被前端轮询到。
        # sync 路径不再直接返回文件（inline-file response）；统一走
        # /api/jobs/{id}/download，多一次 round-trip 但能看到完整进度条。
        _track_task(
            _run_job(
                engine, job_mgr, job, input_path, output_path, out_fmt, actual_quality
            ),
            job_id=job.id,
        )
        out_w, out_h = meta.width * actual_scale, meta.height * actual_scale
        return JSONResponse(
            status_code=202,
            content={
                "job_id": job.id,
                "status": job.status,
                "input_dim": [meta.width, meta.height],
                "scale": actual_scale,
                "estimated_output_dim": [out_w, out_h],
            },
        )

    @app.post("/api/upscale/batch")
    async def upscale_batch(
        images: list[UploadFile] = File(...),
        model: str = Form(...),
        scale: str = Form("4"),
        long_edge: str = Form(""),
        format: str = Form("png"),
        quality: str = Form(""),
    ):
        """批量上传。所有图片共享 model/scale/format 参数；串行处理。"""
        if model not in SUPPORTED_MODELS:
            raise HTTPException(
                422, f"Unsupported model. Allowed: {sorted(SUPPORTED_MODELS)}"
            )
        if not images:
            raise HTTPException(422, "No images uploaded")
        out_fmt = validate_out_format(format)

        # engine 检查在内容校验之前（节省时间，无 binary 直接 503）
        if engine is None:
            raise HTTPException(
                503, "Engine not initialized (binary missing). Check /api/health."
            )
        if len(images) > 50:
            raise HTTPException(413, f"Too many files in batch: {len(images)} > 50")

        # 读 + 校验所有图片（前置错误一次性返回）
        max_bytes = settings.max_upload_mb * 1024 * 1024
        parsed: list[tuple[bytes, tuple[int, int], str]] = []  # (data, dim, ext)
        for idx, img in enumerate(images):
            data = await img.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise HTTPException(
                    413,
                    f"File #{idx + 1} ({img.filename}) too large",
                )
            meta = validate_image_bytes(
                data, max_bytes=max_bytes, max_long_edge=settings.max_long_edge
            )
            parsed.append(
                (
                    data,
                    (meta.width, meta.height),
                    meta.format if meta.format != "jpeg" else "jpg",
                )
            )

        # 计算每个的实际倍率（long_edge 模式按各自 input 长边算）
        # 简化：所有图片共用同一个 scale（除非 long_edge 模式，则各自算）
        if long_edge and long_edge != "0":
            scales = [
                _parse_scale_or_longedge(None, long_edge, w, h)
                for (_, (w, h), _) in parsed
            ]
        else:
            scales = [
                _parse_scale_or_longedge(scale, None, w, h) for (_, (w, h), _) in parsed
            ]

        actual_quality = _parse_quality(quality, out_fmt)

        # 创建 batch + 各自 job
        batch = await batch_mgr.create(total=len(parsed))
        for (data, _, ext), s in zip(parsed, scales):
            job = await job_mgr.create(
                model=model,
                scale=s,
                fmt=out_fmt,
                input_dim=(0, 0),  # batch 模式下不再展开关心
                input_bytes=len(data),
                sync=False,
            )
            await batch_mgr.add_job(batch.id, job.id)
            input_path = Path(job.work_dir) / f"input.{ext}"
            output_path = Path(job.work_dir) / f"output.{out_fmt}"
            await asyncio.to_thread(input_path.write_bytes, data)
            # 串行：每个任务等上一个完成
            _track_task(
                _run_job(
                    engine,
                    job_mgr,
                    job,
                    input_path,
                    output_path,
                    out_fmt,
                    actual_quality,
                ),
                job_id=job.id,
            )

        return JSONResponse(
            status_code=202,
            content={
                "batch_id": batch.id,
                "total": batch.total,
                "job_ids": batch.job_ids,
            },
        )

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str):
        job = await job_mgr.get(job_id)
        if job is None:
            raise HTTPException(404, f"Job not found: {job_id}")
        return job.to_dict()

    @app.delete("/api/jobs/{job_id}")
    async def job_cancel(job_id: str):
        """取消 in-flight 任务。终态 job 幂等返 200。"""
        ok = await job_mgr.cancel(job_id)
        if not ok:
            raise HTTPException(404, f"Job not found: {job_id}")
        return {"job_id": job_id, "status": "cancelled"}

    @app.get("/api/jobs/{job_id}/download")
    async def job_download(job_id: str):
        job = await job_mgr.get(job_id)
        if job is None:
            raise HTTPException(404, f"Job not found: {job_id}")
        if job.status != "done":
            raise HTTPException(409, f"Job status is {job.status}, not done")
        if not job.output_path or not Path(job.output_path).exists():
            raise HTTPException(410, "Output file gone (cleaned up?)")
        w, h = job.output_dim or (0, 0)
        return FileResponse(
            path=job.output_path,
            media_type=content_type_for(job.format),
            filename=f"pixlift_{w}x{h}.{job.format}",
            headers={
                "X-Output-Width": str(w),
                "X-Output-Height": str(h),
            },
        )

    @app.get("/api/batches/{batch_id}")
    async def batch_status(batch_id: str):
        batch = await batch_mgr.get(batch_id)
        if batch is None:
            raise HTTPException(404, f"Batch not found: {batch_id}")
        jobs = []
        done = 0
        failed = 0
        for jid in batch.job_ids:
            j = await job_mgr.get(jid)
            if j is None:
                continue
            jobs.append(j.to_dict())
            if j.status == "done":
                done += 1
            elif j.status == "failed":
                failed += 1
        return {
            "id": batch.id,
            "total": batch.total,
            "done": done,
            "failed": failed,
            "jobs": jobs,
        }

    @app.get("/api/batches/{batch_id}/zip")
    async def batch_zip(batch_id: str):
        """全部 done 的 job 打包成 ZIP 返回；未 done → 409。"""
        batch = await batch_mgr.get(batch_id)
        if batch is None:
            raise HTTPException(404, f"Batch not found: {batch_id}")

        # 检查状态
        out_files: list[Path] = []
        out_names: list[str] = []
        for jid in batch.job_ids:
            j = await job_mgr.get(jid)
            if j is None:
                continue
            if j.status != "done":
                raise HTTPException(
                    409,
                    f"Job {jid} status is {j.status}; wait until all done",
                )
            if j.output_path and Path(j.output_path).exists():
                out_files.append(Path(j.output_path))
                w, h = j.output_dim or (0, 0)
                out_names.append(f"pixlift_{w}x{h}_{j.id[-6:]}.{j.format}")

        if not out_files:
            raise HTTPException(410, "Batch has no outputs")

        # 打包到临时文件
        zip_path = settings.tmp_root / f"{batch.id}.zip"

        # 用线程 offload 避免阻塞 event loop
        def _zip() -> None:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for src, name in zip(out_files, out_names):
                    zf.write(src, name)

        await asyncio.to_thread(_zip)

        from starlette.background import BackgroundTask

        def _cleanup_zip() -> None:
            try:
                zip_path.unlink(missing_ok=True)
            except Exception:
                log.exception("failed to cleanup batch zip: %s", zip_path)

        return FileResponse(
            path=str(zip_path),
            media_type="application/zip",
            filename=f"pixlift_batch_{batch.id[-6:]}.zip",
            background=BackgroundTask(_cleanup_zip),
        )

    return app


async def _run_job(
    engine: Engine,
    mgr: JobManager,
    job,
    input_path: Path,
    output_path: Path,
    out_fmt: str,
    quality: int | None = None,
) -> None:
    """单 job 推理。状态机更新、异常捕获。"""
    try:
        await mgr.update(job.id, status="running", progress=5)
    except Exception:
        log.exception("failed to mark running: %s", job.id)

    # sync/async 都走后台 task（POST 立即返回 job_id，前端轮询）。
    # 这里每个 on_progress 后 sleep(0) 让主 loop 有机会处理 worker thread
    # schedule 进来的其它进度回调（run_coroutine_threadsafe 进的是主 loop 队列，
    # 不是 await 链的一部分）。
    async def on_progress(p: int, phase: str | None = None) -> None:
        # 允许 100（final done），中间帧 clamp 到 [5, 95]（95 后由 _run_job
        # 自己 fire 100，避免 worker 线程的 95 race overwirte done 状态）
        clamped = max(5, min(95, p)) if p < 95 else 95
        if p >= 100:
            clamped = 100
        try:
            await mgr.update(job.id, progress=clamped, phase=phase)
        except Exception:
            log.exception("progress update failed: %s", job.id)
        # 让 worker 调度的其它进度回调有机会跑
        await asyncio.sleep(0)

    started = time.monotonic()
    try:
        result = await engine.upscale(
            input_path=input_path,
            output_path=output_path,
            model=job.model,
            scale=job.scale,
            out_format=out_fmt,
            on_progress=on_progress,
            quality=quality,
        )
        duration = time.monotonic() - started
        await mgr.update(
            job.id,
            status="done",
            progress=100,
            output_path=str(output_path),
            output_dim=list(result.output_dim),
            output_bytes=result.output_bytes,
            finished_at=time.time(),
            duration_s=duration,
            error=None,
        )
    except EngineError as e:
        await mgr.update(
            job.id,
            status="failed",
            finished_at=time.time(),
            duration_s=time.monotonic() - started,
            error=str(e),
        )
    except Exception as e:
        log.exception("unexpected job error: %s", job.id)
        await mgr.update(
            job.id,
            status="failed",
            finished_at=time.time(),
            duration_s=time.monotonic() - started,
            error=f"unexpected: {e}",
        )


async def _periodic_cleanup(mgr: JobManager) -> None:
    """每 10 分钟触发一次 JobManager.cleanup()。"""
    try:
        while True:
            await asyncio.sleep(600)
            try:
                removed = await mgr.cleanup()
                if removed:
                    log.info("periodic cleanup removed %d jobs", removed)
            except Exception:
                log.exception("periodic cleanup failed")
    except asyncio.CancelledError:
        return


# uvicorn pixlift.app:app 入口
app = _make_app()
