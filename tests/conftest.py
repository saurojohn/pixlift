"""Shared pytest fixtures for pixlift tests.

设计要点：
- 模型（100MB+）只在 session 启动时 copy/symlink 一次
- bundled binary 路径不再 rename（避免脏状态泄漏）
- 强制清掉 REAL_ESRGAN_BINARY 环境变量（防止外部 binary 干扰）
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Ensure pixlift package is importable when running pytest from project root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pixlift.app import _make_app  # noqa: E402
from pixlift.config import Settings  # noqa: E402

BIN_DIR = ROOT / "pixlift" / "bin"


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


def _png_bytes(w: int = 128, h: int = 128) -> bytes:
    from io import BytesIO
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (w, h), (120, 200, 60)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path):
    """无 .pth 模型 — engine init 应返 503。"""
    # 确保不引用外部 binary
    os.environ.pop("REAL_ESRGAN_BINARY", None)
    app = _make_app(_settings(tmp_path))
    with TestClient(app) as c:
        yield c


# session scope：模型 .pth 太大，每个 test copy 一次太慢
_SESSION_MODEL_DIR: Path | None = None


def _copy_models_to_session() -> Path:
    global _SESSION_MODEL_DIR
    if _SESSION_MODEL_DIR is not None and _SESSION_MODEL_DIR.exists():
        return _SESSION_MODEL_DIR
    real_models = ROOT / "models"
    target = Path(os.environ.get("PYTEST_SESSION_TMP", "/tmp/pixlift_test_models"))
    if target.exists():
        # 上一轮残留，先清掉
        for p in target.glob("*.pth"):
            p.unlink()
    target.mkdir(parents=True, exist_ok=True)
    if real_models.exists():
        for p in real_models.glob("*.pth"):
            shutil.copy(p, target / p.name)
    _SESSION_MODEL_DIR = target
    return target


@pytest.fixture(scope="session")
def session_model_dir():
    return _copy_models_to_session()


@pytest.fixture
def client_with_model(tmp_path, session_model_dir):
    """PyTorch + 真实 .pth 模型 — 端到端测试用。

    使用 session 级 model_dir（一次性 copy）+ tmp_path 的其余目录。
    """
    os.environ.pop("REAL_ESRGAN_BINARY", None)
    # override model_dir 到 session 共享目录
    app = _make_app(_settings(tmp_path, model_dir=session_model_dir))
    with TestClient(app) as c:
        yield c