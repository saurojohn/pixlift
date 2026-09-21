"""PixLift — lan-only AI image super-resolution web app."""

__version__ = "0.1.0"

from pathlib import Path

# 项目根目录（pyproject.toml 所在）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PIXLIFT_DIR = Path(__file__).resolve().parent
BIN_DIR = PIXLIFT_DIR / "bin"
STATIC_DIR = PIXLIFT_DIR / "static"


def get_binary_path() -> Path | None:
    """解析 realesrgan-ncnn-vulkan 二进制路径。

    优先级：
    1. 环境变量 REAL_ESRGAN_BINARY 显式指定
    2. pixlift/bin/realesrgan-ncnn-vulkan（任务 2 下载/编译产物）
    3. PATH 中的 realesrgan-ncnn-vulkan
    """
    import os
    import shutil

    env = os.environ.get("REAL_ESRGAN_BINARY", "").strip()
    if env:
        p = Path(env).expanduser()
        if p.exists() and os.access(p, os.X_OK):
            return p

    bundled = BIN_DIR / "realesrgan-ncnn-vulkan"
    if bundled.exists() and os.access(bundled, os.X_OK):
        return bundled

    on_path = shutil.which("realesrgan-ncnn-vulkan")
    if on_path:
        return Path(on_path)

    return None
