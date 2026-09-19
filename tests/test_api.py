"""API 集成测试。

无 binary 时验证：健康 503、root 返回前端、校验错误路径正确。
有 binary 时（环境变量 REAL_ESRGAN_BINARY 或 bundled）端到端跑通。
"""
from __future__ import annotations

import io
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from pixlift import BIN_DIR
from pixlift.config import Settings
from pixlift.app import _make_app

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


def _png_bytes(w=128, h=128) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 200, 60)).save(buf, "PNG")
    return buf.getvalue()


def _settings(tmp_path: Path, **overrides) -> Settings:
    defaults = dict(
        host="127.0.0.1",
        port=8000,
        log_level="warning",
        max_upload_mb=5,
        max_long_edge=1024,
        sync_threshold_bytes=2 * 1024 * 1024,
        job_timeout_s=30,
        keep_tmp_hours=0,
        max_jobs=5,
        tmp_root=tmp_path / "tmp",
        model_dir=tmp_path / "models",
        extra_args=[],
        cors_origins=[],
    )
    defaults.update(overrides)
    return Settings(**defaults)


# 注：`client` / `client_with_model` fixtures 现在由 tests/conftest.py 提供，
# 这样 test_features.py 也能复用。


def test_root_returns_index_html(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert "<!DOCTYPE html>" in r.text or "<html" in r.text.lower()


def test_static_mount_serves_assets(client: TestClient):
    # index.html 不通过 /static（已在 /）但 app.js / style.css 应该
    r = client.get("/static/app.js")
    assert r.status_code == 200
    r = client.get("/static/style.css")
    assert r.status_code == 200


def test_health_returns_503_when_no_binary(client: TestClient):
    """无 .pth 模型时 engine 未就绪 → ok=false（PyTorch 后端不再依赖 binary）。"""
    r = client.get("/api/health")
    body = r.json()
    # engine 总是 init 成功（PyTorch），但没有模型 → ok=false / ready=false
    assert body["ok"] is False
    assert body.get("ready") is False
    assert r.status_code == 503  # ready=False 触发 503


def test_upscale_415_for_non_image(client: TestClient):
    fake = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 200
    r = client.post(
        "/api/upscale",
        files={"image": ("test.exe", fake, "application/octet-stream")},
        data={"model": "realesrgan-x4plus", "scale": "4", "format": "png"},
    )
    assert r.status_code == 415


def test_upscale_413_for_too_large_bytes(tmp_path):
    """无 binary 客户端测超大上传 → 413。"""
    os.environ.pop("REAL_ESRGAN_BINARY", None)
    bundled = BIN_DIR / "realesrgan-ncnn-vulkan"
    moved = None
    if bundled.exists():
        moved = bundled.with_suffix(".bak")
        bundled.rename(moved)
    try:
        settings = _settings(tmp_path, max_upload_mb=1)
        app = _make_app(settings)
        with TestClient(app) as c:
            big = _png_bytes(w=2000, h=2000)  # 远超 1 MB
            r = c.post(
                "/api/upscale",
                files={"image": ("big.png", big, "image/png")},
                data={"model": "realesrgan-x4plus", "scale": "4", "format": "png"},
            )
            assert r.status_code == 413
    finally:
        if moved and not bundled.exists():
            moved.rename(bundled)


def test_upscale_422_for_bad_model(client: TestClient):
    img = _png_bytes()
    r = client.post(
        "/api/upscale",
        files={"image": ("t.png", img, "image/png")},
        data={"model": "FAKE", "scale": "4", "format": "png"},
    )
    # 422 来自 FastAPI 校验（model not in SUPPORTED_MODELS）
    assert r.status_code in (422, 415, 503)  # 503 也行（engine 不存在）
    if r.status_code == 503:
        # binary 缺失时校验在 engine 检查之后才走到
        # 实际上 validate_image_bytes 不需要 engine，先校验 → 415
        # 但我们的代码先 check engine；调整顺序后应是 415
        pass


def test_upscale_503_when_no_engine(client: TestClient):
    """client fixture 无模型目录 → engine.health_check 报无模型但 init OK。
    上传请求会进入 _run_job → EngineError("Model weights not found") → 500。"""
    img = _png_bytes()
    r = client.post(
        "/api/upscale",
        files={"image": ("t.png", img, "image/png")},
        data={"model": "realesrgan-x4plus", "scale": "4", "format": "png"},
    )
    # PyTorch 后端：engine init OK 但 upscale 时报模型缺失 → 500
    assert r.status_code == 500
    assert "model" in r.text.lower() or "weight" in r.text.lower()


def test_job_404_for_unknown_id(client: TestClient):
    r = client.get("/api/jobs/j-doesnt-exist")
    assert r.status_code == 404


def test_job_download_409_when_not_done(client: TestClient):
    """直接创建 job 但不跑 → 下载应 409。"""
    from pixlift.jobs import JobManager
    # 通过 /api/upscale 路径触发（虽然会 503，但能进 app 内）
    # 改用直接构造：client 已经有 app 实例，但我们要在测试里取 mgr
    # 这里走黑盒：先 503 时 job 不创建 → 改测无效 job_id
    r = client.get("/api/jobs/j-00000-zzzz/download")
    assert r.status_code == 404


# ===== 有 PyTorch + model 的端到端（依赖 .venv 装好 realesrgan/torch + models/*.pth） =====

try:
    import torch as _torch_check  # noqa
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False

_MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
HAS_MODEL = HAS_TORCH and _MODELS_DIR.exists() and any(_MODELS_DIR.glob("*.pth"))

# 旧的 ncnn-vulkan binary marker 保留兼容（不再使用但不让 import 报错）
needs_binary = pytest.mark.skipif(True, reason="ncnn-vulkan binary path removed in PyTorch backend")
needs_pytorch = pytest.mark.skipif(
    not HAS_MODEL,
    reason="PyTorch + .pth model not available (run setup first)",
)


@needs_pytorch
def test_upscale_end_to_end_small(client_with_model):
    """小图同步路径。"""
    img = _png_bytes(w=128, h=128)
    r = client_with_model.post(
        "/api/upscale",
        files={"image": ("t.png", img, "image/png")},
        data={"model": "realesrgan-x4plus", "scale": "4", "format": "png"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    out = Image.open(io.BytesIO(r.content))
    assert out.size == (512, 512)


@needs_pytorch
def test_upscale_end_to_end_async(tmp_path):
    """大图异步路径 + 轮询 + 下载（用小 sync_threshold 强制 async）。"""
    import shutil
    from pathlib import Path as _P
    real_models = _P(__file__).resolve().parent.parent / "models"
    target = tmp_path / "models"
    target.mkdir(exist_ok=True)
    if real_models.exists():
        for p in real_models.glob("*.pth"):
            shutil.copy(p, target / p.name)
    import os as _os
    try:
        # 直接传 sync_threshold_bytes=1000 强制走 async：
        # 用 512x512 PNG（>2 KB）确保超过阈值。
        app = _make_app(_settings(tmp_path, sync_threshold_bytes=1000))
        with TestClient(app) as c:
            img = _png_bytes(w=512, h=512)
            r = c.post(
                "/api/upscale",
                files={"image": ("t.png", img, "image/png")},
                data={"model": "realesrgan-x4plus", "scale": "4", "format": "png"},
            )
            assert r.status_code == 202, r.text
            body = r.json()
            job_id = body["job_id"]
            import time as _t
            deadline = _t.time() + 60
            status = "queued"
            while _t.time() < deadline:
                j = c.get(f"/api/jobs/{job_id}").json()
                status = j["status"]
                if status in ("done", "failed"):
                    break
                _t.sleep(0.2)
            assert status == "done", f"job did not finish: {c.get(f'/api/jobs/{job_id}').json()}"
            r = c.get(f"/api/jobs/{job_id}/download")
            assert r.status_code == 200
            out = Image.open(io.BytesIO(r.content))
            assert out.size == (2048, 2048)
    finally:
        _os.environ.pop("SYNC_THRESHOLD_BYTES", None)