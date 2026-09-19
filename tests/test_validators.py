"""Validators 测试。"""
from __future__ import annotations

import io

import pytest
from fastapi import HTTPException
from PIL import Image

from pixlift.validators import (
    OUTPUT_FORMATS,
    detect_format,
    validate_image_bytes,
    validate_out_format,
)

FIX = __file__.replace("test_validators.py", "fixtures")


def _png_bytes(w=64, h=64, color=(120, 200, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def _jpg_bytes(w=64, h=64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _webp_bytes(w=64, h=64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 100, 50)).save(buf, "WEBP", quality=90)
    return buf.getvalue()


def _gif_bytes(w=64, h=64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (50, 150, 200)).save(buf, "GIF")
    return buf.getvalue()


def test_detect_format_png():
    assert detect_format(b"\x89PNG\r\n\x1a\nIHDR...") == "png"


def test_detect_format_jpg():
    assert detect_format(b"\xff\xd8\xff\xe0\x00\x10JFIF") == "jpg"


def test_detect_format_webp():
    assert detect_format(b"RIFF\x00\x00\x00\x00WEBPVP8") == "webp"


def test_detect_format_gif():
    assert detect_format(b"GIF89a\x00\x00") == "gif"


def test_detect_format_unknown():
    assert detect_format(b"MZ\x90\x00\x03\x00\x00\x00") is None  # .exe


def test_validate_png_ok():
    meta = validate_image_bytes(_png_bytes(), max_bytes=10_000, max_long_edge=1024)
    assert meta.format == "png"
    assert meta.width == 64 and meta.height == 64


def test_validate_jpg_ok():
    meta = validate_image_bytes(_jpg_bytes(), max_bytes=10_000, max_long_edge=1024)
    assert meta.format == "jpg"
    assert meta.width == 64


def test_validate_webp_ok():
    meta = validate_image_bytes(_webp_bytes(), max_bytes=10_000, max_long_edge=1024)
    assert meta.format == "webp"


def test_validate_gif_ok():
    meta = validate_image_bytes(_gif_bytes(), max_bytes=10_000, max_long_edge=1024)
    assert meta.format == "gif"


def test_validate_exe_rejected():
    fake_exe = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 200
    with pytest.raises(HTTPException) as ei:
        validate_image_bytes(fake_exe, max_bytes=10_000, max_long_edge=1024)
    assert ei.value.status_code == 415


def test_validate_corrupt_png_rejected():
    """magic bytes 对但 PIL 打不开 → 415。"""
    corrupt = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # 不完整的 IHDR
    with pytest.raises(HTTPException) as ei:
        validate_image_bytes(corrupt, max_bytes=10_000, max_long_edge=1024)
    assert ei.value.status_code == 415


def test_validate_too_large_bytes():
    big = _png_bytes(w=2000, h=2000)  # ~几十 KB，不超 max_bytes
    with pytest.raises(HTTPException) as ei:
        validate_image_bytes(big, max_bytes=1000, max_long_edge=4096)
    assert ei.value.status_code == 413


def test_validate_too_long_edge():
    big = _png_bytes(w=2000, h=2000)
    with pytest.raises(HTTPException) as ei:
        validate_image_bytes(big, max_bytes=10_000_000, max_long_edge=1024)
    assert ei.value.status_code == 413
    assert "long edge" in ei.value.detail.lower()


def test_validate_tiny_file():
    with pytest.raises(HTTPException) as ei:
        validate_image_bytes(b"\x89PNG", max_bytes=10_000, max_long_edge=1024)
    assert ei.value.status_code == 415


def test_validate_out_format_ok():
    for f in ("png", "jpg", "webp", "PNG", "JPG"):
        assert validate_out_format(f) in OUTPUT_FORMATS


def test_validate_out_format_bad():
    with pytest.raises(HTTPException) as ei:
        validate_out_format("bmp")
    assert ei.value.status_code == 422