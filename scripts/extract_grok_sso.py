#!/usr/bin/env  python3
"""从已登录的 Grok 浏览器会话中提取 sso token。

用法：
    python scripts/extract_grok_sso.py

脚本会启动一个浏览器窗口，你需要：
1. 手动登录 Grok (如果还没登录)
2. 登录完成后，脚本会自动检测并提取 sso cookie
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# 添加项目路径
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from grok_account_manager.core.browser import DrissionBrowserSession, build_chromium_options


def extract_grok_token():
    """启动浏览器并提取 Grok sso token。"""
    print("[*] 启动浏览器...")
    options = build_chromium_options(lang="zh-CN")
    session = DrissionBrowserSession(options)
    session.start()

    try:
        # 打开 Grok 页面
        grok_url = "https://grok.x.ai/"
        print(f"[*] 正在打开 {grok_url}")
        print("[*] 请在浏览器中完成登录（如果还没登录）")
        session.open_url(grok_url)

        print("\n[*] 等待检测 sso cookie...")
        print("[提示] 如果你已经登录，cookie 应该很快就会被检测到")
        print("[提示] 如果还没登录，请在浏览器中完成登录流程")
        print("[提示] 按 Ctrl+C 可以随时中断\n")

        # 持续检测 sso cookie
        deadline = time.time() + 300  # 5分钟超时
        last_check_time = 0

        while time.time() < deadline:
            try:
                session.refresh_page()
                page = session.page

                if page is None:
                    time.sleep(1)
                    continue

                # 获取所有 cookies
                cookies = page.cookies(all_domains=True, all_info=True) or []
                sso_value = None

                for item in cookies:
                    if isinstance(item, dict):
                        name = str(item.get("name", "")).strip()
                        value = str(item.get("value", "")).strip()
                    else:
                        name = str(getattr(item, "name", "")).strip()
                        value = str(getattr(item, "value", "")).strip()

                    if name == "sso" and value:
                        sso_value = value
                        break

                if sso_value:
                    print(f"\n[✓] 成功提取到 sso token!")
                    print(f"\n{'='*60}")
                    print(f"SSO Token:")
                    print(f"{sso_value}")
                    print(f"{'='*60}\n")

                    # 保存到文件
                    output_dir = PROJECT_ROOT / "output"
                    output_dir.mkdir(exist_ok=True)
                    output_file = output_dir / "extracted_sso.txt"

                    with open(output_file, "a", encoding="utf-8") as f:
                        f.write(f"{sso_value}\n")

                    print(f"[*] Token 已保存到: {output_file}")
                    return sso_value

                # 每5秒显示一次等待提示
                current_time = time.time()
                if current_time - last_check_time >= 5:
                    remaining = int(deadline - current_time)
                    print(f"[*] 仍在等待 sso cookie... (剩余 {remaining} 秒)")
                    last_check_time = current_time

            except KeyboardInterrupt:
                print("\n[*] 用户中断")
                raise
            except Exception as e:
                # 静默处理页面刷新等异常
                pass

            time.sleep(1)

        print("\n[!] 超时：未检测到 sso cookie")
        print("[提示] 请确认你已在浏览器中完成登录")
        return None

    except KeyboardInterrupt:
        print("\n[*] 已取消")
        return None
    finally:
        print("[*] 关闭浏览器...")
        session.stop()


if __name__ == "__main__":
    try:
        token = extract_grok_token()
        if token:
            sys.exit(0)
        else:
            sys.exit(1)
    except Exception as e:
        print(f"\n[错误] {e}")
        sys.exit(1)
