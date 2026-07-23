"""Command-line entry for the FastAPI backend."""

from __future__ import annotations

import argparse

import uvicorn

from ..core.browser import ensure_stable_python_runtime, warn_runtime_compatibility


def run(host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> None:
    ensure_stable_python_runtime()
    warn_runtime_compatibility()
    print(f"[*] MSDSJ grok-account-manager FastAPI 已启动: http://{host}:{port}")
    print("[*] React 开发模式请在 web/ 下运行: npm run dev")
    uvicorn.run(
        "grok_account_manager.api.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 MSDSJ Grok Account Manager FastAPI 后端")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--reload", action="store_true", help="开发模式自动重载")
    args = parser.parse_args()
    run(host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()

