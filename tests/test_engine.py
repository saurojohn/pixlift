"""Engine 测试。

⚠️ 真实子进程测试需要 realesrgan-ncnn-vulkan 二进制；CI/macOS 上若无，
自动跳过（不会失败）。开发机本地有 binary 时自动跑端到端。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from pixlift import BIN_DIR
from pixlift.engine import (
    SUPPORTED_FORMATS,
    SUPPORTED_MODELS,
    SUPPORTED_SCALES,
    Engine,
    EngineError,
)

FIX = Path(__file__).resolve().parent / "fixtures"


def _binary() -> Path | None:
    """Locate binary for tests (env override > bundled > PATH)."""
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
_MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
_HAS_MODEL = _HAS_TORCH and _MODELS_DIR.exists() and any(_MODELS_DIR.glob("*.pth"))

needs_binary = pytest.mark.skipif(True, reason="ncnn-vulkan path removed in PyTorch backend")
needs_pytorch = pytest.mark.skipif(
    not _HAS_MODEL,
    reason="PyTorch + .pth model not available",
)


def test_constants():
    assert "realesrgan-x4plus" in SUPPORTED_MODELS
    assert SUPPORTED_SCALES == (2, 3, 4)
    assert "png" in SUPPORTED_FORMATS


def test_pick_tile_small():
    """256x256 × 4x = 1024 → 用 auto tile。"""
    e = Engine(binary_path=Path("/fake"), model_dir=Path("/fake"))
    assert e._pick_tile(FIX / "small_256.png", scale=4) == 0


def test_pick_tile_large():
    """1500x1500 × 4x = 6000 → 用 tile=256。"""
    # 用大图（已生成 big_1024.jpg，但尺寸还不够；手工给 path 触发启发）
    from PIL import Image

    tmp = FIX / "_big_1500.png"
    if not tmp.exists():
        Image.new("RGB", (1500, 1500), (1, 2, 3)).save(tmp)
    e = Engine(binary_path=Path("/fake"), model_dir=Path("/fake"))
    try:
        assert e._pick_tile(tmp, scale=4) == 256
    finally:
        tmp.unlink(missing_ok=True)


@pytest.mark.skip(reason="ncnn-vulkan subprocess cmd 构造已不适用 PyTorch 后端")
def test_build_cmd_minimum():
    e = Engine(binary_path=Path("/fake/binary"), model_dir=Path("/fake/models"))
    cmd = e._build_cmd(
        input_path=Path("/in.png"),
        output_path=Path("/out.png"),
        model="realesrgan-x4plus",
        scale=4,
        out_format="png",
    )
    assert cmd[0] == "/fake/binary"
    assert "-i" in cmd and "/in.png" in cmd
    assert "-n" in cmd and "realesrgan-x4plus" in cmd
    assert "-s" in cmd and "4" in cmd
    assert "-f" in cmd and "png" in cmd


@pytest.mark.skip(reason="ncnn-vulkan stderr 进度解析已不适用 PyTorch 后端")
def test_progress_regex_parses_percent():
    """_PROGRESS_RE 必须能解析 'XX.X%'。"""
    from pixlift.engine import _PROGRESS_RE
    m = _PROGRESS_RE.search(" 99.5%")
    assert m is not None
    assert float(m.group(1)) == 99.5


@needs_pytorch
def test_end_to_end_small_png_4x(tmp_path):
    """上传 256x256 PNG，4x → 1024x1024 输出。"""
    import shutil
    from pathlib import Path as _P
    real_models = _P(__file__).resolve().parent.parent / "models"
    model_dir = tmp_path / "models"
    model_dir.mkdir(exist_ok=True)
    if real_models.exists():
        for p in real_models.glob("*.pth"):
            shutil.copy(p, model_dir / p.name)
    e = Engine.from_env(binary_path=None, model_dir=model_dir, timeout_s=60)

    progress_calls: list[int] = []

    async def cb(p: int) -> None:
        progress_calls.append(p)

    out = tmp_path / "out.png"

    async def run() -> None:
        await e.upscale(
            input_path=FIX / "small_256.png",
            output_path=out,
            model="realesrgan-x4plus",
            scale=4,
            out_format="png",
            on_progress=cb,
        )

    import asyncio

    asyncio.run(run())

    assert out.exists()
    from PIL import Image

    with Image.open(out) as im:
        w, h = im.size
    assert (w, h) == (1024, 1024)
    # 进度至少触发 1 次（终端一次或 100 收尾）
    assert progress_calls, "progress callback never invoked"


@needs_pytorch
def test_engine_error_on_invalid_model(tmp_path):
    """不支持的 model 应抛 EngineError。"""
    e = Engine.from_env(binary_path=None, model_dir=tmp_path / "m", timeout_s=5)
    import asyncio

    async def run() -> None:
        await e.upscale(
            input_path=FIX / "small_256.png",
            output_path=tmp_path / "out.png",
            model="NOT_A_MODEL",
            scale=4,
            out_format="png",
        )

    with pytest.raises(EngineError, match="Unsupported model"):
        asyncio.run(run())


@needs_pytorch
async def test_health_check(tmp_path):
    """health_check 应返回 ready/models 字段（PyTorch 后端）。"""
    import shutil
    from pathlib import Path as _P
    real_models = _P(__file__).resolve().parent.parent / "models"
    target = tmp_path / "m"
    target.mkdir(exist_ok=True)
    if real_models.exists():
        for p in real_models.glob("*.pth"):
            shutil.copy(p, target / p.name)
    e = Engine.from_env(binary_path=None, model_dir=target)
    info = await e.health_check()
    assert "binary" in info
    assert "version" in info
    assert "ready" in info
    assert isinstance(info["ready"], bool)
    assert isinstance(info["models"], list)