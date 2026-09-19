"""下载/解析 realesrgan-ncnn-vulkan 二进制。

上游 release 只提供 ubuntu 构建（v0.1.3.2，2021-12）。
- Linux: 自动从 GitHub release 下载并解压
- Windows: 自动从 GitHub release 下载并解压
- macOS: 上游无现成 build，提供清晰的编译指引；用户手动编译后用
  环境变量 REAL_ESRGAN_BINARY 指向。

用法：
    python -m pixlift.bin_setup          # 尝试下载
    python -m pixlift.bin_setup --force  # 已存在也重新下载
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

from pixlift import BIN_DIR, __version__ as PIXLIFT_VERSION

# 上游 v0.1.3.2 (2021-12) — 仍是最新稳定 release
ESRGAN_VERSION = "v0.1.3.2"
RELEASE_URL_TEMPLATE = (
    "https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan/releases/download/"
    "{ver}/realesrgan-ncnn-vulkan-{ver}-{platform}.zip"
)


class BinarySetupError(RuntimeError):
    """二进制准备失败。"""


def detect_platform() -> str:
    """映射到上游 zip 文件名里的 platform 字符串。"""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux":
        return "ubuntu"
    if system == "windows":
        return "windows"
    if system == "darwin":
        # 上游无 macOS build（即使 arm64 / x64）
        raise BinarySetupError(
            "macOS has no prebuilt binary. Build from source:\n"
            "  brew install cmake vulkan-headers molten-vk vulkan-loader\n"
            "  git clone https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan\n"
            "  cd Real-ESRGAN-ncnn-vulkan\n"
            "  mkdir build && cd build && cmake ../src && make -j\n"
            "  cp realesrgan-ncnn-vulkan <pixlift>/pixlift/bin/\n"
            "Or set REAL_ESRGAN_BINARY to an existing build."
        )
    raise BinarySetupError(f"Unsupported platform: {system} {machine}")


def download_and_extract(target_dir: Path, force: bool = False) -> Path:
    """下载并解压，返回二进制最终路径。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    plat = detect_platform()
    url = RELEASE_URL_TEMPLATE.format(ver=ESRGAN_VERSION, platform=plat)
    zip_path = target_dir / f"realesrgan-ncnn-vulkan-{plat}.zip"

    print(f"[pixlift] platform={plat}")
    print(f"[pixlift] downloading {url}")

    try:
        urllib.request.urlretrieve(url, zip_path)
    except Exception as e:
        raise BinarySetupError(
            f"Download failed: {e}\n"
            f"Check network or set REAL_ESRGAN_BINARY to an existing binary."
        ) from e

    print(f"[pixlift] extracting to {target_dir}")
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(target_dir)
    finally:
        zip_path.unlink(missing_ok=True)

    binary = target_dir / "realesrgan-ncnn-vulkan"
    if not binary.exists():
        # 可能在子目录
        candidates = list(target_dir.rglob("realesrgan-ncnn-vulkan*"))
        if candidates:
            binary = candidates[0]
            if binary.is_dir():
                binary = next(binary.rglob("realesrgan-ncnn-vulkan"), binary)
        else:
            raise BinarySetupError(
                "Extracted archive does not contain realesrgan-ncnn-vulkan binary"
            )

    if not binary.is_file():
        raise BinarySetupError(f"Expected file at {binary}, got directory")

    # +x（Windows 下会被忽略）
    if platform.system() != "Windows":
        binary.chmod(0o755)
    print(f"[pixlift] binary ready at {binary}")
    return binary


def ensure_binary(force: bool = False) -> Path | None:
    """确保二进制可用，返回路径或 None（macOS 等需要手动）。"""
    env = os.environ.get("REAL_ESRGAN_BINARY", "").strip()
    if env:
        p = Path(env).expanduser()
        if p.exists() and os.access(p, os.X_OK):
            print(f"[pixlift] using REAL_ESRGAN_BINARY={p}")
            return p
        raise BinarySetupError(
            f"REAL_ESRGAN_BINARY={p} is not an executable file"
        )

    if not force and BIN_DIR.exists():
        existing = BIN_DIR / "realesrgan-ncnn-vulkan"
        if existing.exists() and os.access(existing, os.X_OK):
            print(f"[pixlift] using existing binary at {existing}")
            return existing

    try:
        return download_and_extract(BIN_DIR, force=force)
    except BinarySetupError as e:
        print(f"[pixlift] WARNING: {e}", file=sys.stderr)
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Setup Real-ESRGAN binary")
    parser.add_argument(
        "--force", action="store_true", help="re-download even if binary exists"
    )
    args = parser.parse_args()
    print(f"[pixlift {PIXLIFT_VERSION}] binary setup")
    result = ensure_binary(force=args.force)
    if result is None:
        sys.exit(1)
    print(f"[pixlift] OK: {result}")


if __name__ == "__main__":
    main()