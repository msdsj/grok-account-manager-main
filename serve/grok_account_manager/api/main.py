"""Command-line entry for the FastAPI backend."""

from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn

from ..core.browser import ensure_stable_python_runtime, warn_runtime_compatibility


def run(host: str = "127.0.0.1", port: int = 43187, reload: bool = False, open_browser: bool = False) -> None:
    ensure_stable_python_runtime()
    warn_runtime_compatibility()
    url = f"http://{host}:{port}"
    print(f"[*] MSDSJ grok-account-manager 已启动: {url}")
    print("[*] 注册、账号、三网关与前端都在这一条进程内；无需 Docker 或额外服务。")
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
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
    parser.add_argument("--port", type=int, default=43187, help="监听端口")
    parser.add_argument("--reload", action="store_true", help="开发模式自动重载")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()
    run(host=args.host, port=args.port, reload=args.reload, open_browser=args.open)


if __name__ == "__main__":
    main()
