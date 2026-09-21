"""环境变量配置（集中管理 + 单一来源）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"
    max_upload_mb: int = 20
    max_long_edge: int = 4096
    sync_threshold_bytes: int = 2 * 1024 * 1024  # < 2MB 同步
    job_timeout_s: int = 120
    keep_tmp_hours: int = 0  # 0 = 不保留历史；>0 启动时清超出时长目录
    max_jobs: int = 50
    max_batches: int = 30  # 内存中保留 batch 元数据数量上限
    max_concurrent_upscales: int = 1  # 同时推理上限（=1 串行；MPS 内存受限时可保持 1）
    tmp_root: Path = field(default_factory=lambda: Path("tmp"))
    model_dir: Path = field(default_factory=lambda: Path("models"))
    extra_args: list[str] = field(default_factory=list)
    cors_origins: list[str] = field(default_factory=list)


def _list_env(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return list(default)
    out = []
    for s in raw.split(","):
        s = s.strip().strip('"').strip("'").strip()
        if s:
            out.append(s)
    return out


def _get_int(name: str, default: int, lo: int, hi: int) -> int:
    """读 int 环境变量，超出范围 raise。"""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        v = int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be int, got {raw!r}") from e
    if v < lo or v > hi:
        raise ValueError(f"{name}={v} out of range [{lo}, {hi}]")
    return v


def load_settings() -> Settings:
    """从环境变量构造 Settings，未设置取默认值。

    环境变量超出合法范围会抛 ValueError（启动时失败而不是运行时崩）。
    """
    cors = _list_env("ALLOWED_ORIGINS", [])
    # 通配符必须显式配 credentials=False；如果用户写 `*`，警告 + 拒收
    if "*" in cors:
        raise ValueError(
            "ALLOWED_ORIGINS=* is not allowed for security; "
            "list explicit origins or use a separate dev mode."
        )
    return Settings(
        host=os.environ.get("HOST", "0.0.0.0").strip(),
        port=_get_int("PORT", 8000, 1, 65535),
        log_level=os.environ.get("LOG_LEVEL", "info").strip(),
        max_upload_mb=_get_int("MAX_UPLOAD_MB", 20, 1, 1024),
        max_long_edge=_get_int("MAX_LONG_EDGE", 4096, 64, 16384),
        sync_threshold_bytes=_get_int(
            "SYNC_THRESHOLD_BYTES", 2 * 1024 * 1024, 1024, 100 * 1024 * 1024
        ),
        job_timeout_s=_get_int("JOB_TIMEOUT_S", 120, 1, 86400),
        keep_tmp_hours=_get_int("KEEP_TMP_HOURS", 0, 0, 720),
        max_jobs=_get_int("MAX_JOBS", 50, 1, 10000),
        max_batches=_get_int("MAX_BATCHES", 30, 1, 10000),
        max_concurrent_upscales=_get_int("MAX_CONCURRENT_UPSCALES", 1, 1, 32),
        tmp_root=Path(os.environ.get("TMP_DIR", "tmp").strip()),
        model_dir=Path(os.environ.get("MODEL_DIR", "models").strip()),
        extra_args=_list_env("REAL_ESRGAN_EXTRA_ARGS", []),
        cors_origins=cors,
    )
