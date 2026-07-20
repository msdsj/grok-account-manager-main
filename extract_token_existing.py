#!/usr/bin/env python3
"""连接到现有的 Chrome/Chromium 浏览器并提取 Grok sso token。

前置条件：
    浏览器必须以调试模式启动，命令示例：

    macOS:
    /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222

    Windows:
    chrome.exe --remote-debugging-port=9222

    Linux:
    google-chrome --remote-debugging-port=9222

用法：
    python extract_token_existing.py [--port 9222]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from DrissionPage import Chromium


def extract_token_from_existing_browser(port: int = 9222) -> str | None:
    """连接到现有浏览器并提取 sso token。"""
    try:
        print(f"[*] 尝试连接到端口 {port} 的浏览器...")
        browser = Chromium(addr_or_opts=f"127.0.0.1:{port}")

        print("[*] 成功连接到浏览器")
        tabs = browser.get_tabs()

        if not tabs:
            print("[!] 浏览器中没有打开的标签页")
            return None

        print(f"[*] 找到 {len(tabs)} 个标签页")

        # 尝试在所有标签页中查找 sso cookie
        for i, tab in enumerate(tabs, 1):
            try:
                url = tab.url
                print(f"[*] 检查标签页 {i}: {url[:60]}...")

                # 获取所有 cookies
                cookies = tab.cookies(all_domains=True, all_info=True) or []

                for item in cookies:
                    if isinstance(item, dict):
                        name = str(item.get("name", "")).strip()
                        value = str(item.get("value", "")).strip()
                        domain = str(item.get("domain", "")).strip()
                    else:
                        name = str(getattr(item, "name", "")).strip()
                        value = str(getattr(item, "value", "")).strip()
                        domain = str(getattr(item, "domain", "")).strip()

                    if name == "sso" and value and "x.ai" in domain:
                        print(f"\n[✓] 在标签页 {i} 中找到 sso token!")
                        print(f"    域名: {domain}")
                        print(f"\n{'='*60}")
                        print(f"SSO Token:")
                        print(f"{value}")
                        print(f"{'='*60}\n")
                        return value

            except Exception as e:
                print(f"[!] 检查标签页 {i} 时出错: {e}")
                continue

        print("\n[!] 在所有标签页中都未找到 sso cookie")
        print("[提示] 请确认:")
        print("  1. 你已在浏览器中打开 https://grok.x.ai/ 并登录")
        print("  2. 浏览器是以调试模式启动的")
        return None

    except ConnectionRefusedError:
        print(f"\n[错误] 无法连接到端口 {port}")
        print("\n[解决方案] 请使用调试模式启动 Chrome/Chromium:")
        print("\nmacOS:")
        print('  /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222')
        print("\nWindows:")
        print('  chrome.exe --remote-debugging-port=9222')
        print("\nLinux:")
        print('  google-chrome --remote-debugging-port=9222')
        print("\n然后重新运行此脚本")
        return None
    except Exception as e:
        print(f"\n[错误] {e}")
        return None


def save_token(token: str, output_format: str = "txt"):
    """保存 token 到文件。"""
    project_root = Path(__file__).resolve().parent
    output_dir = project_root / "output"
    output_dir.mkdir(exist_ok=True)

    if output_format == "json":
        # 保存为 JSON 格式 (兼容 tokens-all-all-all.json 格式)
        output_file = output_dir / "extracted_tokens.json"

        # 读取现有数据或创建新数据
        if output_file.exists():
            try:
                with open(output_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {"basic": []}
        else:
            data = {"basic": []}

        # 检查是否已存在
        token_exists = any(
            item.get("token") == token
            for item in data.get("basic", [])
        )

        if not token_exists:
            data["basic"].append({"token": token, "tags": []})
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"[*] Token 已保存到: {output_file}")
        else:
            print(f"[*] Token 已存在于: {output_file}")
    else:
        # 保存为纯文本格式
        output_file = output_dir / "extracted_sso.txt"
        with open(output_file, "a", encoding="utf-8") as f:
            f.write(f"{token}\n")
        print(f"[*] Token 已保存到: {output_file}")


def main():
    parser = argparse.ArgumentParser(
        description="从现有浏览器提取 Grok sso token"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9222,
        help="浏览器调试端口 (默认: 9222)"
    )
    parser.add_argument(
        "--format",
        choices=["txt", "json"],
        default="json",
        help="输出格式 (默认: json)"
    )

    args = parser.parse_args()

    token = extract_token_from_existing_browser(port=args.port)

    if token:
        save_token(token, output_format=args.format)
        return 0
    else:
        return 1


if __name__ == "__main__":
    sys.exit(main())
