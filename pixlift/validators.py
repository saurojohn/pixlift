"""上传校验：magic bytes + 尺寸 + 大小。

所有失败抛 HTTPException，FastAPI 直接渲染 JSON 错误响应。
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError

# 支持的格式（按 magic bytes 区分）。
# 注意：RIFF 容器有多种（WAV/AVI/CDXA），光看开头 4 字节 "RIFF" 不够；WebP
# 必须看 8-12 字节是否是 "WEBP"。
MAGIC_BYTES = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    # JPEG: SOI marker 总是 \xff\xd8\xff（不同 APP marker 不影响 magic）
    "jpg": (b"\xff\xd8\xff",),
    "webp": None,  # 用 _is_webp() 特殊处理（要查 8-12 字节）
    "gif": (b"GIF87a", b"GIF89a"),
}

# 输出格式白名单（V1: png/jpg/webp）
OUTPUT_FORMATS = frozenset({"png", "jpg", "webp"})

# 解压炸弹防御上限：最大像素数（防止攻击者上传声明超大的 PNG）。
# MAX_IMAGE_PIXELS 上限 = 4096*4096*4 = 67M（4 通道）；远低于真实 OOM 阈值
DEFAULT_MAX_IMAGE_PIXELS = 100_000_000


@dataclass(frozen=True)
class ImageMeta:
    format: str  # png/jpg/webp/gif
    width: int
    height: int
    bytes_size: int


def _is_webp(head: bytes) -> bool:
    """WebP 必须是 RIFF 容器且 8-12 字节为 'WEBP'。"""
    return len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP"


def detect_format(head: bytes) -> str | None:
    """通过 magic bytes 判断格式，返回 'png'/'jpg'/'webp'/'gif' 或 None。

    注意：head 至少需要 12 字节才能识别 WebP。
    """
    if _is_webp(head):
        return "webp"
    for fmt, sigs in MAGIC_BYTES.items():
        if sigs is None:
            continue
        for sig in sigs:
            if head.startswith(sig):
                return fmt
    return None


def _install_decompression_bomb_guard(max_long_edge: int) -> None:
    """设 PIL 全局上限 = max_long_edge^2 * 4，超过即抛 DecompressionBombError。"""
    cap = max(DEFAULT_MAX_IMAGE_PIXELS, (max_long_edge ** 2) * 4)
    # 调高（不能调低）以兼容更大的输入
    if Image.MAX_IMAGE_PIXELS is None or cap > Image.MAX_IMAGE_PIXELS:
        Image.MAX_IMAGE_PIXELS = cap


def validate_image_bytes(
    data: bytes,
    *,
    max_bytes: int,
    max_long_edge: int,
) -> ImageMeta:
    """校验 bytes 是合法图片，且 size/尺寸在限制内。

    抛 HTTPException(415/413/...)，返回 ImageMeta。
    """
    if len(data) > max_bytes:
        mb = max_bytes // (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"Image too large: {len(data)} bytes > {max_bytes} ({mb} MB limit)",
        )

    # 最小 12 字节才能识别 WebP 容器
    if len(data) < 12:
        raise HTTPException(
            status_code=415, detail="File too small to be an image"
        )

    fmt = detect_format(data[:12])
    if fmt is None:
        raise HTTPException(
            status_code=415,
            detail="Unsupported file format. Allowed: PNG, JPG, WebP, GIF.",
        )

    # 解压炸弹防御（每次调都设置，保证线程安全的"上调"语义）
    _install_decompression_bomb_guard(max_long_edge)

    # 两阶段：先 verify()（不分配像素），再 fresh open 读 size + format
    # verify() 会让 instance 进入 unusable 状态，必须重开。
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
            pil_format_verify = im.format
    except (UnidentifiedImageError, SyntaxError, OSError, ValueError) as e:
        raise HTTPException(
            status_code=415, detail=f"Corrupt or unreadable image: {e}"
        ) from e

    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            pil_format = (im.format or "").lower()
    except (UnidentifiedImageError, SyntaxError, OSError, ValueError) as e:
        raise HTTPException(
            status_code=415, detail=f"Corrupt or unreadable image: {e}"
        ) from e

    # MIME 交叉校验：magic bytes 标的是 png，但 PIL 解析出是 jpeg → 攻击
    expected = {"jpg": "jpeg"}.get(fmt, fmt)
    if pil_format and pil_format != expected and pil_format != fmt:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Format mismatch: magic bytes say '{fmt}' but decoder says "
                f"'{pil_format}'. Refusing to process."
            ),
        )
    if pil_format_verify and pil_format_verify.lower() not in {"png", "jpeg", "gif", "webp"}:
        raise HTTPException(
            status_code=415, detail=f"Unsupported decoded format: {pil_format_verify}"
        )

    if w <= 0 or h <= 0:
        raise HTTPException(status_code=415, detail="Image has zero dimension")

    if max(w, h) > max_long_edge:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Image too large: {w}x{h}, long edge > {max_long_edge} px. "
                "Downscale first."
            ),
        )

    return ImageMeta(format=fmt, width=w, height=h, bytes_size=len(data))


def validate_out_format(fmt: str) -> str:
    """校验输出格式。"""
    f = fmt.lower().strip()
    if f not in OUTPUT_FORMATS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported output format: {fmt}. Allowed: {sorted(OUTPUT_FORMATS)}",
        )
    return f


def content_type_for(fmt: str) -> str:
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "webp": "image/webp",
    }[fmt]