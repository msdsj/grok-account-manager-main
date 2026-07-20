"""注册后的  OAuth 授权助手。

在自动注册完成后，引导用户手动完成 OAuth 授权以获取完整凭证。

使用方式：
1. 运行 grok-account-manager 完成自动注册
2. 运行此脚本，使用已注册的账号手动授权
3. 获取完整的 OAuth tokens 和凭证信息
"""

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

from .client import fetch_complete_credential


# OAuth 配置
OIDC_ISSUER = "https://auth.x.ai"
OIDC_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OIDC_SCOPE = "openid profile email offline_access grok-cli:access api:access conversations:read conversations:write"

DEVICE_AUTHORIZATION_ENDPOINT = "https://auth.x.ai/oauth2/device/code"
TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"


def _verification_url(device_data: dict) -> str:
    complete = (device_data.get("verification_uri_complete") or "").strip()
    if complete:
        return complete
    user_code = device_data["user_code"]
    return_to = quote(f"/oauth2/device?user_code={user_code}", safe="")
    return (
        "https://accounts.x.ai/sign-in"
        f"?redirect=oauth2-provider&return_to={return_to}&email=true"
    )


def authorize_account(email: str, profile: dict | None = None) -> dict | None:
    """为已注册的账号获取 OAuth tokens（需要手动授权）。

    Args:
        email: 已注册的邮箱
        profile: 注册时的个人资料（可选）

    Returns:
        完整的凭证 JSON，或 None（如果失败）
    """
    print(f"\n{'='*60}")
    print(f"为账号 {email} 获取完整凭证")
    print(f"{'='*60}\n")

    # 步骤 1: 发起 device authorization
    try:
        print("[1/3] 发起 OAuth Device Flow...")
        device_response = requests.post(
            DEVICE_AUTHORIZATION_ENDPOINT,
            data={
                "client_id": OIDC_CLIENT_ID,
                "scope": OIDC_SCOPE,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        device_response.raise_for_status()
        device_data = device_response.json()

        device_code = device_data["device_code"]
        user_code = device_data["user_code"]
        verification_url = _verification_url(device_data)
        interval = device_data.get("interval", 5)
        expires_in = device_data.get("expires_in", 900)

        print(f"\n✓ Device Code 已生成")
        print(f"  User Code: {user_code}")

    except Exception as e:
        print(f"\n✗ 获取 device code 失败: {e}")
        return None

    # 步骤 2: 提示用户手动授权
    print(f"\n[2/3] 请在浏览器中完成授权")
    print(f"\n{'─'*60}")
    print(f"  1. 使用账号 {email} 登录 x.ai")
    print(f"  2. 访问授权页面:")
    print(f"     {verification_url}")
    print(f"  3. 确认授权（允许访问）")
    print(f"  4. 回到这里，程序会自动继续...")
    print(f"{'─'*60}\n")

    # 自动打开浏览器（可选）
    try:
        import webbrowser
        webbrowser.open(verification_url)
        print("✓ 已在浏览器中打开授权页面\n")
    except Exception:
        print("⚠ 无法自动打开浏览器，请手动访问上述 URL\n")

    # 步骤 3: 轮询 token endpoint
    print(f"[3/3] 等待授权确认...")
    print(f"（将在 {expires_in} 秒后超时）\n")

    max_attempts = int(expires_in / interval) + 1
    for attempt in range(max_attempts):
        try:
            token_response = requests.post(
                TOKEN_ENDPOINT,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                    "client_id": OIDC_CLIENT_ID,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )

            if token_response.status_code == 200:
                oauth_tokens = token_response.json()
                print("\n✓ 授权成功！正在获取完整凭证...\n")

                # 获取完整凭证
                full_credential = fetch_complete_credential(
                    email=email,
                    sso_token=oauth_tokens["access_token"],
                    profile=profile,
                    oauth_tokens=oauth_tokens,
                )

                print(f"✓ 成功获取完整凭证")
                print(f"  User ID: {full_credential.get('user_id', 'N/A')}")
                print(f"  Plan: {full_credential.get('plan_type', 'N/A')}")
                print(f"  Has Grok Code: {full_credential.get('has_grok_code_access', 'N/A')}")

                return full_credential

            error_data = token_response.json()
            error_code = error_data.get("error", "")

            if error_code == "authorization_pending":
                # 继续等待
                remaining = (max_attempts - attempt - 1) * interval
                print(f"  等待授权... ({remaining} 秒后超时)", end="\r")
                time.sleep(interval)
                continue
            elif error_code == "slow_down":
                # 降低轮询频率
                interval += 5
                time.sleep(interval)
                continue
            elif error_code == "access_denied":
                print(f"\n✗ 授权被拒绝")
                return None
            elif error_code == "expired_token":
                print(f"\n✗ Device code 已过期")
                return None
            else:
                print(f"\n✗ Token 请求失败: {error_code}")
                return None

        except Exception as e:
            print(f"\n✗ 轮询 token 失败: {e}")
            time.sleep(interval)

    print(f"\n✗ 授权超时")
    return None


def main():
    parser = argparse.ArgumentParser(
        description="为已注册的 Grok 账号获取完整 OAuth 凭证"
    )
    parser.add_argument("email", help="已注册的邮箱地址")
    parser.add_argument("--output-dir", default="output/credentials", help="凭证输出目录")
    parser.add_argument("--profile-json", help="个人资料 JSON 文件路径（可选）")
    args = parser.parse_args()

    # 读取 profile（如果提供）
    profile = None
    if args.profile_json:
        try:
            with open(args.profile_json, "r") as f:
                profile = json.load(f)
        except Exception as e:
            print(f"警告：无法读取 profile 文件: {e}")

    # 执行授权
    credential = authorize_account(args.email, profile)

    if credential:
        # 保存凭证
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        import hashlib
        email_hash = hashlib.md5(args.email.encode()).hexdigest()[:8]
        timestamp = credential.get("created_at", int(time.time() * 1000))
        filename = f"grok_{timestamp}_{email_hash}.json"
        filepath = output_dir / filename

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump([credential], f, ensure_ascii=False, indent=2)

        print(f"\n{'='*60}")
        print(f"✓ 完整凭证已保存到: {filepath}")
        print(f"{'='*60}\n")
        return 0
    else:
        print(f"\n{'='*60}")
        print(f"✗ 获取凭证失败")
        print(f"{'='*60}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
