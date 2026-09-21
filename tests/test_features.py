"""V1.1 新功能测试：resolve_scale + quality 后处理 + batch/long_edge 校验路径。"""
from __future__ import annotations

import io
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from pixlift import BIN_DIR
from pixlift.app import _make_app
from pixlift.config import Settings
from pixlift.engine import (
    SUPPORTED_LONG_EDGES,
    SUPPORTED_SCALES,
    Engine,
    resolve_scale,
)

FIX = Path(__file__).resolve().parent / "fixtures"


def _binary() -> Path | None:
    env = os.environ.get("REAL_ESRGAN_BINARY", "").strip()
    if env and Path(env).exists():
        return Path(env)
    bundled = BIN_DIR / "realesrgan-ncnn-vulkan"
    if bundled.exists():
        return bundled
    on_path = shutil.which("realesrgan-ncnn-vulkan")
    return Path(on_path) if on_path else None


BIN = _binary()
# needs_pytorch: PyTorch 后端端到端
try:
    import torch as _torch_check  # noqa
    _HAS_TORCH = True
except ImportError:
    _HAS_TORCH = False
from pathlib import Path as _P
_MODELS_DIR = _P(__file__).resolve().parent.parent / "models"
_HAS_MODEL = _HAS_TORCH and _MODELS_DIR.exists() and any(_MODELS_DIR.glob("*.pth"))
needs_binary = pytest.mark.skipif(True, reason="ncnn-vulkan removed")
needs_pytorch = pytest.mark.skipif(not _HAS_MODEL, reason="PyTorch + .pth model not available")


# ===== resolve_scale (纯函数) =====


def test_resolve_scale_exact_match():
    """目标尺寸正好等于 input 长边 → 最小档 = 2（避免 no-op）。"""
    # 500 → 2048 ratio 4.096 → 4x
    assert resolve_scale(2048, 500) == 4
    # 1000 → 2048 ratio 2.048 → 3x (>= ratio)
    assert resolve_scale(2048, 1000) == 3
    # 800 → 1536 ratio 1.92 → 2x
    assert resolve_scale(1536, 800) == 2
    # 500 → 4096 ratio 8.192 → 4x (上限)
    assert resolve_scale(4096, 500) == 4
    # 1000 → 1024 ratio 1.024 → 2x
    assert resolve_scale(1024, 1000) == 2
    # 1000 → 500 ratio 0.5 (目标小于输入) → 2x（仍然 upscale）
    assert resolve_scale(500, 1000) == 2


def test_resolve_scale_max_cap():
    """目标超过 4x 能力时封顶 4x。"""
    assert resolve_scale(4096, 100) == 4


# ===== quality 后处理（需要 binary 验证 PIL 重编码）=====


@needs_pytorch
def test_quality_reencode_jpg_smaller_than_png(tmp_path):
    """JPG quality=70 应明显小于同尺寸 PNG。"""
    import asyncio, shutil
    from pathlib import Path as _P
    real_models = _P(__file__).resolve().parent.parent / "models"
    model_dir = tmp_path / "m"
    model_dir.mkdir(exist_ok=True)
    if real_models.exists():
        for p in real_models.glob("*.pth"):
            shutil.copy(p, model_dir / p.name)
    e = Engine.from_env(binary_path=None, model_dir=model_dir, timeout_s=30)
    out = tmp_path / "out.jpg"

    async def run() -> None:
        await e.upscale(
            input_path=FIX / "small_256.png",
            output_path=out,
            model="realesrgan-x4plus",
            scale=4,
            out_format="jpg",
            quality=70,
        )

    asyncio.run(run())
    assert out.exists()
    size_jpg = out.stat().st_size

    out2 = tmp_path / "out2.png"
    async def run_png() -> None:
        await e.upscale(
            input_path=FIX / "small_256.png",
            output_path=out2,
            model="realesrgan-x4plus",
            scale=4,
            out_format="png",
        )
    asyncio.run(run_png())
    size_png = out2.stat().st_size
    # JPG q70 通常远小于 PNG
    assert size_jpg < size_png, f"JPG {size_jpg} should be smaller than PNG {size_png}"


@needs_pytorch
def test_quality_reencode_png_ignores_quality(tmp_path):
    """PNG 是无损，quality 参数被忽略（不报错）。"""
    import asyncio, shutil
    from pathlib import Path as _P
    real_models = _P(__file__).resolve().parent.parent / "models"
    model_dir = tmp_path / "m"
    model_dir.mkdir(exist_ok=True)
    if real_models.exists():
        for p in real_models.glob("*.pth"):
            shutil.copy(p, model_dir / p.name)
    e = Engine.from_env(binary_path=None, model_dir=model_dir, timeout_s=30)
    out = tmp_path / "out.png"

    async def run() -> None:
        await e.upscale(
            input_path=FIX / "small_256.png",
            output_path=out,
            model="realesrgan-x4plus",
            scale=4,
            out_format="png",
            quality=50,  # 应被忽略
        )

    asyncio.run(run())
    assert out.exists()
    with Image.open(out) as im:
        assert im.format == "PNG"


# ===== API: long_edge 参数 =====


def _png(w=64, h=64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 100, 80)).save(buf, "PNG")
    return buf.getvalue()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8000,
        log_level="warning",
        max_upload_mb=5,
        max_long_edge=4096,
        sync_threshold_bytes=2 * 1024 * 1024,
        job_timeout_s=30,
        keep_tmp_hours=0,
        max_jobs=10,
        tmp_root=tmp_path / "tmp",
        model_dir=tmp_path / "models",
        extra_args=[],
        cors_origins=[],
    )


@pytest.fixture
def client(tmp_path):
    """无 binary 客户端。"""
    os.environ.pop("REAL_ESRGAN_BINARY", None)
    bundled = BIN_DIR / "realesrgan-ncnn-vulkan"
    moved = None
    if bundled.exists():
        moved = bundled.with_suffix(".bak")
        bundled.rename(moved)
    try:
        app = _make_app(_settings(tmp_path))
        with TestClient(app) as c:
            yield c
    finally:
        if moved and not bundled.exists():
            moved.rename(bundled)


def _wait_job_finished(client: TestClient, job_id: str, timeout_s: float = 15.0) -> dict:
    """等后台 _run_job 跑到终态（done/failed）。返回最终 job dict。"""
    import time as _t
    deadline = _t.time() + timeout_s
    status = None
    while _t.time() < deadline:
        j = client.get(f"/api/jobs/{job_id}").json()
        status = j["status"]
        if status in ("done", "failed"):
            return j
        _t.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish in {timeout_s}s (last status={status})")


def test_upscale_accepts_long_edge_param(client: TestClient):
    """long_edge 参数合法时走通校验到后台 _run_job → 无模型 → status=failed。"""
    r = client.post(
        "/api/upscale",
        files={"image": ("t.png", _png(800, 600), "image/png")},
        data={
            "model": "realesrgan-x4plus",
            "scale": "",
            "long_edge": "2048",
            "format": "png",
        },
    )
    # 统一走 async：POST 立即 202 + job_id，后台任务跑 engine → 无模型 → failed
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    final = _wait_job_finished(client, job_id)
    assert final["status"] == "failed"


def test_long_edge_takes_priority_over_scale(client: TestClient):
    """两者都传时 long_edge 优先（设计决策：避免歧义）。"""
    r = client.post(
        "/api/upscale",
        files={"image": ("t.png", _png(800, 600), "image/png")},
        data={
            "model": "realesrgan-x4plus",
            "scale": "4",
            "long_edge": "2048",
            "format": "png",
        },
    )
    # 长边 2048 + 输入 800 → 3x → 应走通到 engine check（无模型 → 任务 failed）
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    final = _wait_job_finished(client, job_id)
    assert final["status"] == "failed"


def test_upscale_rejects_bad_long_edge(client: TestClient):
    """long_edge 不在白名单 → 422。"""
    r = client.post(
        "/api/upscale",
        files={"image": ("t.png", _png(), "image/png")},
        data={
            "model": "realesrgan-x4plus",
            "long_edge": "9999",
            "format": "png",
        },
    )
    assert r.status_code == 422


def test_upscale_accepts_quality_param(client: TestClient):
    """quality 参数合法。"""
    r = client.post(
        "/api/upscale",
        files={"image": ("t.png", _png(), "image/png")},
        data={
            "model": "realesrgan-x4plus",
            "scale": "4",
            "format": "jpg",
            "quality": "85",
        },
    )
    # 统一走 async：POST 202 + job_id；engine OK 但 upscale 无模型 → failed
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    final = _wait_job_finished(client, job_id)
    assert final["status"] == "failed"


def test_upscale_rejects_bad_quality(client: TestClient):
    """quality=150 → 422。"""
    r = client.post(
        "/api/upscale",
        files={"image": ("t.png", _png(), "image/png")},
        data={
            "model": "realesrgan-x4plus",
            "scale": "4",
            "format": "jpg",
            "quality": "150",
        },
    )
    assert r.status_code == 422


# ===== API: batch 端点 =====


def test_batch_rejects_empty(client: TestClient):
    r = client.post(
        "/api/upscale/batch",
        files=[],
        data={"model": "realesrgan-x4plus", "scale": "4", "format": "png"},
    )
    assert r.status_code == 422


def test_batch_rejects_too_many(client_with_model: TestClient):
    """>50 张 → 413 数量限制（用 client_with_model 确保 engine OK）。"""
    files = [
        ("images", (f"t{i}.png", _png(), "image/png")) for i in range(51)
    ]
    r = client_with_model.post(
        "/api/upscale/batch",
        files=files,
        data={"model": "realesrgan-x4plus", "scale": "4", "format": "png"},
    )
    assert r.status_code == 413


def test_batch_503_without_engine(client: TestClient):
    """client fixture 无模型 → batch 接受请求（202），但 job 内部 EngineError
    "Model weights not found"（本地）/ "PyTorch not installed"（CI）→ job.status = "failed"。
    测试只关心 batch 接受请求 + 所有 job 进入 failed 终态。"""
    files = [
        ("images", (f"t{i}.png", _png(), "image/png")) for i in range(3)
    ]
    r = client.post(
        "/api/upscale/batch",
        files=files,
        data={"model": "realesrgan-x4plus", "scale": "4", "format": "png"},
    )
    assert r.status_code == 202
    body = r.json()
    bid = body["batch_id"]
    # 等任务完成（失败也算完成）
    import time as _t
    deadline = _t.time() + 30
    while _t.time() < deadline:
        batch = client.get(f"/api/batches/{bid}").json()
        if all(j["status"] in ("done", "failed") for j in batch["jobs"]):
            break
        _t.sleep(0.2)
    assert batch["failed"] == len(batch["jobs"]), f"expected all jobs failed: {batch}"
    # 错误信息可以是 "weight"（本地）/ "pytorch"/"installed"（CI）
    err_text = " ".join((j.get("error") or "") for j in batch["jobs"]).lower()
    assert any(s in err_text for s in ("model", "weight", "pytorch", "installed", "not found"))


def test_batch_rejects_non_image(client: TestClient):
    """batch 中有 .exe → magic bytes 校验返 415（client fixture 无模型但校验先发生）。"""
    fake_exe = b"MZ\x90\x00" + b"\x00" * 100
    files = [
        ("images", ("ok.png", _png(), "image/png")),
        ("images", ("bad.exe", fake_exe, "application/octet-stream")),
    ]
    r = client.post(
        "/api/upscale/batch",
        files=files,
        data={"model": "realesrgan-x4plus", "scale": "4", "format": "png"},
    )
    # batch 路由：model 校验 → engine check → file 校验 → 上传文件
    # 因为 client 无模型目录 engine init OK，但上传时 bad.exe 走 magic bytes 校验
    # 实际顺序：engine check OK (PyTorch) → file 校验 → bad.exe → 415
    assert r.status_code == 415


def test_batch_404_for_unknown(client: TestClient):
    r = client.get("/api/batches/b-nope")
    assert r.status_code == 404


def test_batch_zip_404_for_unknown(client: TestClient):
    r = client.get("/api/batches/b-nope/zip")
    assert r.status_code == 404


# ===== /api/health 长边列表 =====


def test_health_includes_long_edges(client: TestClient):
    r = client.get("/api/health")
    body = r.json()
    assert "supported_long_edges" in body
    assert set(body["supported_long_edges"]) == set(SUPPORTED_LONG_EDGES)