"""Real-ESRGAN PyTorch 后端（替代 ncnn-vulkan binary）。

使用 `realesrgan` Python 包（官方 PyTorch 实现），通过 in-process Python 调用
替代原来的 subprocess 方式。API 表面保持兼容（Engine.upscale / health_check
签名不变），所以 app.py 和 tests 完全不需要改。

设计要点：
- 模型懒加载（首次 upscale() 时才 init，进程启动快）
- 模型缓存：同 (model_name, scale, fp32) 复用同一个 Upsampler
- quality 后处理：JPG/WebP 用 Pillow 重编码（与原方案一致）
- 进度通过 on_progress 回调（每 N 步触发一次，因为 PyTorch 不像 binary 那样打百分比）
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Literal

from PIL import Image, UnidentifiedImageError

# 模型清单（与原 ncnn 版本一致；realesrgan Python 包用的名字相同）
SUPPORTED_MODELS = frozenset(
    {
        "realesrgan-x4plus",
        "realesrgan-x4plus-anime",
        "realesr-animevideov3",
        "realesrnet-x4plus",
    }
)
SUPPORTED_SCALES = (2, 3, 4)
SUPPORTED_FORMATS = ("png", "jpg", "webp")

# long_edge 模式支持的目标尺寸（px）
SUPPORTED_LONG_EDGES = (1024, 1536, 2048, 3072, 4096)

# 输出像素阈值（长边 × scale 超过此值 → 用更小 tile 省显存）
_TILE_DOWNGRADE_PIXELS = 4000

# 进度 ring buffer（兼容原 API 保留 stderr tail）
_STDERR_RING_MAXLEN = 50

ProgressCb = Callable[[int], Awaitable[None]] | Callable[[int], None]


class EngineError(RuntimeError):
    """Real-ESRGAN 推理失败。"""


@dataclass
class EngineResult:
    returncode: int
    output_path: Path
    output_dim: tuple[int, int]
    output_bytes: int
    stderr_tail: str = ""  # deprecated, use log_tail; kept for API compat
    log_tail: str = ""
    duration_s: float = 0.0


def resolve_scale(long_edge: int, input_long_edge: int) -> int:
    """给定目标长边 + 输入长边，返回最接近的实际倍率。

    倍率取最小能 >= 目标的整档（2/3/4）。若目标 <= 输入，返回 2（最小放大）。
    Real-ESRGAN 是放大模型，不支持降采样。
    """
    if long_edge <= input_long_edge:
        return 2
    ratio = long_edge / input_long_edge
    for s in SUPPORTED_SCALES:
        if s >= ratio:
            return s
    return SUPPORTED_SCALES[-1]


class _UpsamplerCache:
    """Upsampler 缓存：同 (model_name, tile_size) 复用同一实例。

    注意：被 `Engine.upscale` 在 `asyncio.to_thread` worker 线程里并发调用。
    用 `threading.Lock` 保护 `_cache[key]` 的读-改-写，避免冷启动时重复实例化。
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, int, bool], object] = {}
        self._lock = threading.Lock()

    def get(
        self,
        model_name: str,
        tile_size: int,
        fp32: bool,
        model_dir: Path,
        device: object | None = None,
    ) -> object:
        key = (model_name, tile_size, fp32)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            # realesrgan 包内 import：延后到 Engine.__init__ 之后的首次 upscale
            from realesrgan import RealESRGANer
            from basicsr.archs.rrdbnet_arch import RRDBNet
            from basicsr.archs.srvgg_arch import SRVGGNetCompact

            # 根据模型名选 architecture
            model_cls = SRVGGNetCompact if "animevideov3" in model_name else RRDBNet
            # 不同模型的 network 配置（与官方权重匹配）
            if "animevideov3" in model_name:
                # SRVGGNetCompact（轻量）
                model_inst = model_cls(num_in_ch=3, num_out_ch=3, num_feat=64, num_conv=16, upscale=4, act_type="prelu")
            elif "x4plus-anime" in model_name:
                # RealESRGAN_x4plus_anime_6B.pth 是 6 个 RRDB blocks（小网络）
                model_inst = model_cls(num_in_ch=3, num_out_ch=3, scale=4, num_feat=64, num_block=6, num_grow_ch=32)
            elif "x4plus" in model_name:
                # RealESRGAN_x4plus.pth / RealESRNet_x4plus.pth 都是 23 个 RRDB blocks
                model_inst = model_cls(num_in_ch=3, num_out_ch=3, scale=4, num_feat=64, num_block=23, num_grow_ch=32)
            else:
                model_inst = model_cls(num_in_ch=3, num_out_ch=3, scale=4, num_feat=64, num_block=23, num_grow_ch=32)

            upsampler = RealESRGANer(
                scale=_scale_for_model(model_name),
                model_path=_resolve_model_path(model_name, model_dir),
                model=model_inst,
                tile=tile_size,
                tile_pad=10,
                pre_pad=0,
                half=(not fp32) and device is not None and device.type == "cuda",
                device=device,
            )
            self._cache[key] = upsampler
            return upsampler


def _scale_for_model(model_name: str) -> int:
    """所有模型在 ncnn 版本里都是 4x；realesr-animevideov3 在 python 包里也是 4x。"""
    return 4


# 模型文件别名：logical_name -> [可能的 .pth 文件名]
# 让用户既能放小写名（pixlift 习惯），也能放上游官方驼峰名（Real-ESRGAN release）。
_ALIASES = {
    "realesrgan-x4plus": ["realesrgan-x4plus.pth", "RealESRGAN_x4plus.pth"],
    "realesrgan-x4plus-anime": [
        "realesrgan-x4plus-anime.pth",
        "RealESRGAN_x4plus_anime.pth",
        "RealESRGAN_x4plus_anime_6B.pth",
    ],
    "realesr-animevideov3": [
        "realesr-animevideov3.pth",
        "RealESRGANv2-animevideo-xsx4.pth",
    ],
    "realesrnet-x4plus": [
        "realesrnet-x4plus.pth",
        "RealESRNet_x4plus.pth",
    ],
}


def _resolve_model_path(model_name: str, model_dir: Path) -> str:
    """返回模型权重路径，缺失则抛 EngineError。

    支持多种命名约定：
    - 小写：realesrgan-x4plus-anime.pth
    - 官方驼峰：RealESRGAN_x4plus_anime_6B.pth（x4plus-anime）
    - 官方驼峰：RealESRNet_x4plus.pth（realesrnet-x4plus）
    """
    candidates = [model_dir / f"{model_name}.pth"]
    for name in _ALIASES.get(model_name, []):
        candidates.append(model_dir / name)
    for c in candidates:
        if c.exists():
            return str(c)
    raise EngineError(
        f"Model weights not found: tried {[c.name for c in candidates]}. "
        f"Download from https://github.com/xinntao/Real-ESRGAN/releases "
        f"and place the .pth file in {model_dir}."
    )


@dataclass
class Engine:
    binary_path: Path | None  # 保留字段兼容旧代码，但不再使用
    model_dir: Path
    timeout_s: int = 120
    extra_args: list[str] = field(default_factory=list)
    _upsampler_cache: _UpsamplerCache = field(default_factory=_UpsamplerCache, init=False, repr=False)
    _torch_dtype: object = field(default=None, init=False, repr=False)
    _torch_device: object = field(default=None, init=False, repr=False)
    _initialized: bool = field(default=False, init=False, repr=False)
    _semaphore: asyncio.Semaphore | None = field(default=None, init=False, repr=False)

    def set_max_concurrent(self, n: int) -> None:
        """限制同时推理数（防 N 张大图同时打爆 MPS 内存）。"""
        if n < 1:
            raise ValueError(f"max_concurrent must be >= 1, got {n}")
        self._semaphore = asyncio.Semaphore(n)

    @classmethod
    def from_env(
        cls,
        binary_path: Path | None,
        model_dir: Path,
        timeout_s: int | None = None,
        extra_args: list[str] | None = None,
    ) -> "Engine":
        # 不再需要 binary_path（保留参数兼容旧调用点；忽略）
        return cls(
            binary_path=binary_path,
            model_dir=model_dir,
            timeout_s=timeout_s if timeout_s is not None else int(os.environ.get("JOB_TIMEOUT_S", "120")),
            extra_args=list(extra_args) if extra_args else [],
        )

    def _ensure_torch(self) -> None:
        """首次 upscale() 时检查 torch 可用并探测精度（mps/cuda/cpu）。"""
        if self._initialized:
            return
        try:
            import torch
        except ImportError as e:
            raise EngineError(
                "PyTorch not installed. Run: pip install torch realesrgan basicsr"
            ) from e
        # 选择 backend
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            # Apple Silicon Metal
            self._torch_dtype = torch.float16 if not _env_fp32() else torch.float32
            self._torch_device = torch.device("mps")
        elif torch.cuda.is_available():
            self._torch_dtype = torch.float16 if not _env_fp32() else torch.float32
            self._torch_device = torch.device("cuda")
        else:
            self._torch_dtype = torch.float32
            self._torch_device = torch.device("cpu")
        self._initialized = True

    def _pick_tile(self, input_path: Path, scale: int) -> int:
        """根据输出长边选择 tile-size（0 = auto）。"""
        try:
            with Image.open(input_path) as im:
                w, h = im.size
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
            # 文件损坏 / 不可读 — 留给 upscale 阶段抛 EngineError；这里只 tile
            return 0
        long_edge = max(w, h)
        return 256 if long_edge * scale > _TILE_DOWNGRADE_PIXELS else 0

    async def upscale(
        self,
        input_path: Path,
        output_path: Path,
        model: str,
        scale: int,
        out_format: Literal["png", "jpg", "webp"],
        on_progress: ProgressCb | None = None,
        quality: int | None = None,
    ) -> EngineResult:
        if model not in SUPPORTED_MODELS:
            raise EngineError(f"Unsupported model: {model}")
        if scale not in SUPPORTED_SCALES:
            raise EngineError(f"Unsupported scale: {scale} (expected 2/3/4)")
        if out_format not in SUPPORTED_FORMATS:
            raise EngineError(f"Unsupported format: {out_format}")
        if quality is not None and not (1 <= quality <= 100):
            raise EngineError(f"quality must be 1..100, got {quality}")
        effective_quality = quality if out_format in ("jpg", "webp") else None

        self._ensure_torch()
        tile = self._pick_tile(input_path, scale)

        # 进度回调：异步任务中触发（这里同步跑，转发回调）
        stderr_lines: deque[str] = deque(maxlen=_STDERR_RING_MAXLEN)
        stderr_lines.append(f"engine=python-realesrgan device={self._torch_device} tile={tile}")

        # 捕获当前 event loop，用于 worker 线程里 schedule 异步进度回调
        loop = asyncio.get_running_loop()

        # 进度 + 阶段：simple linear approximation（PyTorch 没百分比输出）
        last_pct = 0
        current_phase = ""

        def _fire_phase(p: int, phase: str) -> None:
            """线程安全地触发 on_progress（worker 线程版：fire-and-forget）。

            on_progress 可能是 sync 或 async：
            - sync：直接调
            - async：从 worker 线程 schedule 到主 loop，不阻塞推理
            """
            nonlocal last_pct, current_phase
            p_clamped = max(last_pct, min(99, p))
            if p_clamped > last_pct or phase != current_phase:
                if p_clamped > last_pct:
                    last_pct = p_clamped
                current_phase = phase
                if not on_progress:
                    return
                try:
                    res = on_progress(p_clamped, phase)  # type: ignore[arg-type]
                except TypeError:
                    # 老回调只接 (p,)
                    res = on_progress(p_clamped)  # type: ignore[call-arg]
                if asyncio.iscoroutine(res):
                    # worker 线程：把 coroutine 交给主 loop
                    try:
                        asyncio.run_coroutine_threadsafe(res, loop)
                    except RuntimeError:
                        # loop 已关闭（应用关停中），放弃这次回调
                        pass

        async def _fire_phase_async(p: int, phase: str) -> None:
            """主 loop 版：保证 on_progress 完成才返回。

            用于 engine.upscale 末尾的进度回调（要 await 避免 race）。
            """
            nonlocal last_pct, current_phase
            p_clamped = max(last_pct, min(99, p))
            if p_clamped > last_pct or phase != current_phase:
                if p_clamped > last_pct:
                    last_pct = p_clamped
                current_phase = phase
                if on_progress:
                    try:
                        res = on_progress(p_clamped, phase)  # type: ignore[arg-type]
                    except TypeError:
                        res = on_progress(p_clamped)  # type: ignore[call-arg]
                    if asyncio.iscoroutine(res):
                        await res

        start = time.monotonic()

        # 阶段 1：模型加载（30%）
        _fire_phase(5, "load_model")

        # 在线程里跑同步推理（避免阻塞 event loop）
        def _do_upscale() -> None:
            # 阶段 2：预处理 + 加载 upsampler（这步慢，主要是 torch.load）
            import torch as _t
            upsampler = self._upsampler_cache.get(
                model_name=model,
                tile_size=tile,
                fp32=(self._torch_dtype == _t.float32),
                model_dir=self.model_dir,
                device=self._torch_device,
            )
            _fire_phase(30, "preprocess")
            # realesrgan 在内部用 torch.no_grad()
            with _t.inference_mode():
                # realesrgan.enhance 期望 numpy BGR 数组
                import numpy as _np
                import cv2 as _cv2
                with Image.open(input_path) as _im:
                    _img_pil = _im.convert("RGB")
                _img_bgr = _cv2.cvtColor(_np.array(_img_pil), _cv2.COLOR_RGB2BGR)
                _fire_phase(40, "infer")
                _out_bgr, _ = upsampler.enhance(_img_bgr, outscale=scale)
                _out_rgb = _cv2.cvtColor(_out_bgr, _cv2.COLOR_RGB2BGR)
                _img_out = Image.fromarray(_out_rgb)
                _fire_phase(85, "save")
                # 转换格式
                save_kwargs: dict = {}
                save_format = out_format.upper() if out_format != "jpg" else "JPEG"
                if out_format == "jpg":
                    if _img_out.mode in ("RGBA", "LA", "P"):
                        _img_out = _img_out.convert("RGB")
                    save_kwargs = {"quality": effective_quality or 95, "optimize": True}
                elif out_format == "webp" and effective_quality:
                    save_kwargs = {"quality": effective_quality, "method": 4}
                output_path.parent.mkdir(parents=True, exist_ok=True)
                _img_out.save(output_path, save_format, **save_kwargs)

        try:
            # 用 semaphore 限制并发推理数（防 N 张大图同时打爆 MPS 内存）
            sem = self._semaphore
            if sem is not None:
                async with sem:
                    await asyncio.wait_for(
                        asyncio.to_thread(_do_upscale),
                        timeout=self.timeout_s,
                    )
            else:
                await asyncio.wait_for(
                    asyncio.to_thread(_do_upscale),
                    timeout=self.timeout_s,
                )
        except asyncio.TimeoutError as e:
            raise EngineError(
                f"Engine timeout after {self.timeout_s}s"
            ) from e
        except Exception as e:
            raise EngineError(f"realesrgan upscale failed: {e}") from e

        # 95% 是 save 阶段，await 确保 on_progress 完成（而不是 run_coroutine_threadsafe）
        # 否则 worker 线程的 schedule 可能跟主 loop 里的 done/progress=100 race
        await _fire_phase_async(95, "save")

        duration = time.monotonic() - start
        stderr_lines.append(f"done in {duration:.1f}s")

        # 探测输出尺寸
        try:
            with Image.open(output_path) as im:
                w, h = im.size
        except Exception as e:
            raise EngineError(f"Cannot read output {output_path}: {e}") from e

        size = output_path.stat().st_size

        if on_progress:
            try:
                res = on_progress(100, "done")  # type: ignore[arg-type]
            except TypeError:
                res = on_progress(100)  # type: ignore[call-arg]
            if asyncio.iscoroutine(res):
                await res

        return EngineResult(
            returncode=0,
            output_path=output_path,
            output_dim=(w, h),
            output_bytes=size,
            stderr_tail="\n".join(stderr_lines),
            log_tail="\n".join(stderr_lines),
            duration_s=duration,
        )

    async def health_check(self) -> dict:
        """探测后端可用性、模型、Vulkan 状态（兼容字段）。"""
        info: dict = {
            "binary": "python-realesrgan",
            "version": ESRGAN_PROBE_VERSION,
            "vulkan": False,
            "gpu": "unknown",
            "models": [],
            "model_files": [],  # 实际 .pth 文件名（诊断用）
            "ready": False,
        }
        try:
            self._ensure_torch()
            info["vulkan"] = False  # PyTorch 后端不再有 Vulkan 概念
            info["gpu"] = f"{self._torch_device} ({self._torch_dtype})"
            # 就绪条件：torch 可用 + 至少 1 个 .pth 模型
            files = list(self.model_dir.glob("*.pth")) if self.model_dir.exists() else []
            info["model_files"] = sorted(p.name for p in files)
            # 解析为逻辑模型名（前端比对用的 key）
            available_logical: set[str] = set()
            for f in files:
                for logical, aliases in _ALIASES.items():
                    if f.name in aliases or f.name == f"{logical}.pth":
                        available_logical.add(logical)
            info["models"] = sorted(available_logical)
            info["ready"] = len(files) > 0
        except EngineError as e:
            info["gpu"] = f"unavailable: {e}"
        except Exception as e:
            info["gpu"] = f"probe failed: {e}"

        return info


def _env_fp32() -> bool:
    return os.environ.get("REAL_ESRGAN_FP32", "").strip().lower() in ("1", "true", "yes")


# 探测用的常量（兼容旧 API）
ESRGAN_PROBE_VERSION = "realesrgan-py"