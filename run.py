#!/usr/bin/env python3
"""PixLift 启动入口。

Usage:
    python run.py
    python run.py --host 0.0.0.0 --port 8000
    uvicorn pixlift.app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import argparse
import os

import uvicorn


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PixLift — AI image upscaling web app")
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="1-65535",
    )
    p.add_argument("--reload", action="store_true", help="dev mode autoreload (use with care)")
    p.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "info"),
        choices=["critical", "error", "warning", "info", "debug", "trace"],
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if not args.host:
        raise SystemExit("--host cannot be empty")
    if not (1 <= args.port <= 65535):
        raise SystemExit(f"--port must be 1-65535, got {args.port}")
    uvicorn.run(
        "pixlift.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()