"""使用 sso cookie (access_token) 调用 xAI API 获取完整账号信息。

参考 cockpit-tools 的实现，调用以下 API：
- /v1/billing - 账单和配额
- /v1/user - 用户信息和订阅
- /rest/subscriptions - 订阅详情
- /rest/tasks/usage - 任务使用情况

如果 API 调用失败（401），尝试从 sso cookie (JWT) 中解析用户信息。
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import time
from typing import TypedDict
import uuid

import requests


BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
CLI_USER_URL = "https://cli-chat-proxy.grok.com/v1/user?include=subscription"
SUBSCRIPTIONS_URL = "https://grok.com/rest/subscriptions"
TASK_USAGE_URL = "https://grok.com/rest/tasks/usage"
DEFAULT_CLIENT_VERSION = "0.2.93"
OIDC_ISSUER = "https://auth.x.ai"
OIDC_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"


def decode_jwt_claims(token: str) -> dict:
    """解析 JWT token，提取 payload 中的 claims。

    Args:
        token: JWT token 字符串

    Returns:
        解析后的 claims 字典，解析失败返回空字典
    """
    try:
        # JWT 格式: header.payload.signature
        parts = token.split('.')
        if len(parts) != 3:
            return {}

        # 解码 payload (第二部分)
        payload = parts[1]
        # 添加 padding（如果需要）
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += '=' * padding

        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception as e:
        print(f"[警告] JWT 解析失败: {e}")
        return {}


class GrokCredential(TypedDict, total=False):
    """完整的 Grok 凭证信息，匹配 cockpit-tools 的 GrokAccount 结构。"""
    # 基本信息
    id: str
    email: str
    auth_mode: str  # "oauth" 或 "api_key"
    created_at: int
    last_used: int

    # OAuth tokens
    access_token: str
    refresh_token: str | None
    id_token: str | None
    token_type: str | None
    expires_at: int | None
    expires_at_raw: str | None

    # OIDC 配置
    oidc_issuer: str | None
    oidc_client_id: str | None
    token_endpoint: str | None

    # 用户身份信息
    user_id: str | None
    principal_id: str | None
    principal_type: str | None
    team_id: str | None
    first_name: str | None
    last_name: str | None
    profile_image_asset_id: str | None
    coding_data_retention_opt_out: bool | None

    # 订阅和配额
    plan_type: str | None
    subscription_tier: str | None
    subscription_status: str | None
    has_grok_code_access: bool | None
    quota: dict | None

    # 原始 API 响应（用于调试和后续处理）
    auth_raw: dict | None
    billing_raw: dict | None
    subscription_raw: dict | None
    user_raw: dict | None
    task_usage_raw: dict | None

    # 更新时间
    usage_updated_at: int | None

    # 额外信息
    profile: dict | None


def _iso_utc(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _non_empty(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _set_text_if_present(target: dict, key: str, value) -> None:
    text = _non_empty(value)
    if text is not None:
        target[key] = text


def _set_bool_if_present(target: dict, key: str, value) -> None:
    if isinstance(value, bool):
        target[key] = value


def build_cockpit_grok_credential(
    email: str,
    access_token: str,
    profile: dict | None = None,
    oauth_tokens: dict | None = None,
) -> GrokCredential:
    """构造 cockpit-tools 可直接导入的 GrokAccount JSON 对象。

    注意：即使只有浏览器里的 sso/JWT，也不要把它标成 api_key。
    cockpit-tools 的 Grok 导入逻辑在 `auth_mode=api_key` 时会强校验
    `api_key` 字段；注册器产出的浏览器 token 属于 OAuth 会话凭据。
    """
    now_ts = int(time.time())
    now_ms = int(time.time() * 1000)
    token = (oauth_tokens or {}).get("access_token") or access_token
    token = _non_empty(token) or ""

    credential: GrokCredential = {
        "id": str(uuid.uuid4()),
        "email": _non_empty(email) or "unknown@grok.local",
        "auth_mode": "oauth",
        "first_name": None,
        "last_name": None,
        "user_id": None,
        "principal_id": None,
        "principal_type": None,
        "team_id": None,
        "access_token": token,
        "refresh_token": (oauth_tokens or {}).get("refresh_token"),
        "id_token": (oauth_tokens or {}).get("id_token"),
        "token_type": (oauth_tokens or {}).get("token_type") or "Bearer",
        "expires_at": None,
        "expires_at_raw": None,
        "oidc_issuer": OIDC_ISSUER,
        "oidc_client_id": OIDC_CLIENT_ID,
        "token_endpoint": _non_empty((oauth_tokens or {}).get("token_endpoint")) or TOKEN_ENDPOINT,
        "plan_type": None,
        "quota": None,
        "auth_raw": None,
        "billing_raw": None,
        "subscription_raw": None,
        "user_raw": None,
        "task_usage_raw": None,
        "has_grok_code_access": None,
        "usage_updated_at": now_ms,
        "created_at": now_ms,
        "last_used": now_ms,
    }

    if oauth_tokens and oauth_tokens.get("expires_in"):
        try:
            credential["expires_at"] = now_ts + int(oauth_tokens["expires_in"])
            credential["expires_at_raw"] = _iso_utc(credential["expires_at"])
        except (TypeError, ValueError):
            pass

    if oauth_tokens and oauth_tokens.get("redirect_uri"):
        credential["auth_raw"] = {
            "redirect_uri": _non_empty(oauth_tokens.get("redirect_uri")),
        }

    # access_token 与 id_token 都可能携带身份 claims；后者通常含 email/name。
    _extract_jwt_claims(credential, decode_jwt_claims(token))
    if oauth_tokens and oauth_tokens.get("id_token"):
        _extract_jwt_claims(credential, decode_jwt_claims(oauth_tokens["id_token"]))

    if profile:
        _set_text_if_present(
            credential,
            "first_name",
            credential.get("first_name") or profile.get("first_name") or profile.get("given_name"),
        )
        _set_text_if_present(
            credential,
            "last_name",
            credential.get("last_name") or profile.get("last_name") or profile.get("family_name"),
        )

    _finalize_cockpit_grok_credential(credential)
    return credential


def fetch_complete_credential(
    email: str,
    sso_token: str,
    profile: dict | None = None,
    oauth_tokens: dict | None = None,
    client_version: str = DEFAULT_CLIENT_VERSION,
    timeout: int = 30
) -> GrokCredential:
    """使用 sso cookie 或 OAuth tokens 获取完整的 Grok 凭证信息。

    Args:
        email: 注册邮箱
        sso_token: 从浏览器获取的 sso cookie 值（或 OAuth access_token）
        profile: 注册时填写的个人资料 (first_name, last_name 等)
        oauth_tokens: OAuth token 响应（如果有），包含 access_token, refresh_token, id_token 等
        client_version: Grok CLI 客户端版本号
        timeout: 请求超时时间（秒）

    Returns:
        GrokCredential: 包含完整信息的凭证对象
    """
    # 如果有 OAuth tokens，使用它们；否则使用 sso_token
    access_token = oauth_tokens["access_token"] if oauth_tokens else sso_token
    has_oauth_tokens = oauth_tokens is not None

    credential = build_cockpit_grok_credential(
        email=email,
        access_token=access_token,
        profile=profile,
        oauth_tokens=oauth_tokens,
    )

    # 尝试调用各个 API 获取完整信息
    # 只有在有真正的 OAuth access_token 时才会成功
    api_success = False

    # 尝试 Authorization header 方式
    try:
        billing_data = _fetch_billing(access_token, client_version, timeout)
        if not billing_data and not has_oauth_tokens:
            billing_data = _fetch_billing_with_cookie(access_token, client_version, timeout)
        if billing_data:
            credential["billing_raw"] = billing_data
            _extract_quota_from_billing(credential, billing_data)
            api_success = True
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401 and not has_oauth_tokens:
            # 401 错误且没有 OAuth tokens，尝试使用 Cookie 方式
            try:
                billing_data = _fetch_billing_with_cookie(access_token, client_version, timeout)
                if billing_data:
                    credential["billing_raw"] = billing_data
                    _extract_quota_from_billing(credential, billing_data)
                    api_success = True
            except Exception as e2:
                print(f"[警告] Cookie 方式获取 billing 也失败: {e2}")
        elif e.response.status_code == 401:
            print(f"[警告] OAuth token 获取 billing 失败（401）: {e}")
        else:
            print(f"[警告] 获取 billing 信息失败: {e}")
    except Exception as e:
        print(f"[警告] 获取 billing 信息失败: {e}")

    # 如果 API 调用成功（或有 OAuth tokens），继续获取其他信息
    if api_success or has_oauth_tokens:
        try:
            user_data = _fetch_user(access_token, client_version, timeout)
            if user_data:
                credential["user_raw"] = user_data
                _extract_user_info(credential, user_data)
        except Exception as e:
            print(f"[警告] 获取 user 信息失败: {e}")

        try:
            subscription_data = _fetch_subscriptions(
                access_token,
                credential.get("user_id"),
                client_version,
                timeout
            )
            if subscription_data:
                credential["subscription_raw"] = subscription_data
                _extract_subscription_info(credential, subscription_data)
        except Exception as e:
            print(f"[警告] 获取 subscription 信息失败: {e}")

        try:
            task_usage_data = _fetch_task_usage(access_token, timeout)
            if task_usage_data:
                credential["task_usage_raw"] = task_usage_data
                _extract_task_usage(credential, task_usage_data)
        except Exception as e:
            print(f"[警告] 获取 task usage 信息失败: {e}")

    _finalize_cockpit_grok_credential(credential)

    return credential


def _extract_jwt_claims(credential: GrokCredential, claims: dict) -> None:
    """从 JWT claims 中提取用户信息到 credential。"""
    if not claims:
        return

    # 常见的 JWT claims 字段
    if "email" in claims and not credential.get("email"):
        credential["email"] = claims["email"]

    if "sub" in claims:
        credential["principal_id"] = claims["sub"]
        if not credential.get("user_id"):
            credential["user_id"] = claims["sub"]

    if "user_id" in claims or "uid" in claims:
        credential["user_id"] = claims.get("user_id") or claims.get("uid")

    if "principal_id" in claims:
        credential["principal_id"] = claims["principal_id"]

    if "principal_type" in claims:
        credential["principal_type"] = claims["principal_type"]

    if "team_id" in claims:
        credential["team_id"] = claims["team_id"]

    if "given_name" in claims or "first_name" in claims:
        credential["first_name"] = claims.get("first_name") or claims.get("given_name")

    if "family_name" in claims or "last_name" in claims:
        credential["last_name"] = claims.get("last_name") or claims.get("family_name")

    if "profile_image_asset_id" in claims or "picture" in claims:
        credential["profile_image_asset_id"] = claims.get("profile_image_asset_id") or claims.get("picture")

    if "coding_data_retention_opt_out" in claims:
        _set_bool_if_present(
            credential,
            "coding_data_retention_opt_out",
            claims.get("coding_data_retention_opt_out"),
        )

    if "exp" in claims:
        try:
            credential["expires_at"] = int(claims["exp"])
            credential["expires_at_raw"] = _iso_utc(credential["expires_at"])
        except (ValueError, TypeError):
            pass


def _finalize_cockpit_grok_credential(credential: GrokCredential) -> None:
    """补齐 cockpit GrokAccount 需要的派生字段与 auth_raw。"""
    now_ms = int(time.time() * 1000)

    credential["id"] = _non_empty(credential.get("id")) or str(uuid.uuid4())
    credential["email"] = _non_empty(credential.get("email")) or "unknown@grok.local"
    credential["auth_mode"] = "oauth"
    credential["access_token"] = _non_empty(credential.get("access_token")) or ""
    credential["created_at"] = int(credential.get("created_at") or now_ms)
    credential["last_used"] = int(credential.get("last_used") or credential["created_at"])
    credential["usage_updated_at"] = int(credential.get("usage_updated_at") or now_ms)
    credential["token_type"] = _non_empty(credential.get("token_type")) or "Bearer"
    credential["oidc_issuer"] = _non_empty(credential.get("oidc_issuer")) or OIDC_ISSUER
    credential["oidc_client_id"] = _non_empty(credential.get("oidc_client_id")) or OIDC_CLIENT_ID
    credential["token_endpoint"] = _non_empty(credential.get("token_endpoint")) or TOKEN_ENDPOINT

    if not credential.get("principal_type"):
        credential["principal_type"] = "User"
    if credential.get("principal_id") and not credential.get("user_id"):
        credential["user_id"] = credential["principal_id"]
    if credential.get("user_id") and not credential.get("principal_id"):
        credential["principal_id"] = credential["user_id"]

    if credential.get("expires_at") and not credential.get("expires_at_raw"):
        try:
            credential["expires_at_raw"] = _iso_utc(int(credential["expires_at"]))
        except (TypeError, ValueError):
            credential["expires_at_raw"] = None

    quota = credential.get("quota")
    if isinstance(quota, dict):
        quota.setdefault("products", [])
        subscription_tier = credential.get("subscription_tier")
        subscription_status = credential.get("subscription_status")
        if subscription_tier and not quota.get("subscriptionTier"):
            quota["subscriptionTier"] = subscription_tier
        if subscription_status and not quota.get("subscriptionStatus"):
            quota["subscriptionStatus"] = subscription_status
        if quota.get("subscriptionTier") and not credential.get("plan_type"):
            credential["plan_type"] = quota.get("subscriptionTier")

    if credential.get("subscription_tier") and not credential.get("plan_type"):
        credential["plan_type"] = credential.get("subscription_tier")

    # GrokAccount 没有 subscription_tier/status 顶层字段；保留在 quota 里。
    credential.pop("subscription_tier", None)
    credential.pop("subscription_status", None)
    credential.pop("profile", None)

    create_time = _iso_utc(credential["created_at"] // 1000)
    auth_raw = credential.get("auth_raw") if isinstance(credential.get("auth_raw"), dict) else {}
    auth_raw.update({
        "key": credential.get("access_token"),
        "auth_mode": "oidc",
        "create_time": auth_raw.get("create_time") or create_time,
        "email": credential.get("email"),
        "first_name": credential.get("first_name"),
        "last_name": credential.get("last_name"),
        "profile_image_asset_id": credential.get("profile_image_asset_id"),
        "principal_type": credential.get("principal_type"),
        "principal_id": credential.get("principal_id"),
        "team_id": credential.get("team_id"),
        "user_id": credential.get("user_id"),
        "coding_data_retention_opt_out": credential.get("coding_data_retention_opt_out") or False,
        "refresh_token": credential.get("refresh_token"),
        "expires_at": credential.get("expires_at_raw"),
        "oidc_issuer": credential.get("oidc_issuer"),
        "oidc_client_id": credential.get("oidc_client_id"),
        "token_endpoint": credential.get("token_endpoint"),
        "redirect_uri": (credential.get("auth_raw") or {}).get("redirect_uri")
        or (credential.get("auth_raw") or {}).get("redirectURI"),
        "token_type": credential.get("token_type"),
    })
    if isinstance(credential.get("auth_raw"), dict):
        redirect_uri = _non_empty(credential["auth_raw"].get("redirect_uri"))
        if redirect_uri:
            auth_raw["redirect_uri"] = redirect_uri
    credential["auth_raw"] = auth_raw


def _fetch_billing_with_cookie(access_token: str, client_version: str, timeout: int) -> dict | None:
    """使用 Cookie 方式获取账单和配额信息。"""
    try:
        response = requests.get(
            BILLING_URL,
            headers={
                "Cookie": f"sso={access_token}",
                "Accept": "application/json",
                "x-xai-token-auth": "xai-grok-cli",
                "x-grok-cli-version": client_version,
                "x-grok-client-version": client_version,
                "x-grok-client-surface": "grok-cli",
                "x-grok-client-identifier": "grok-account-manager",
                "User-Agent": f"grok-cli/{client_version}",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"[错误] Cookie 方式 billing API 请求失败: {e}")
        return None


def _fetch_billing(access_token: str, client_version: str, timeout: int) -> dict | None:
    """获取账单和配额信息。"""
    try:
        response = requests.get(
            BILLING_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "x-xai-token-auth": "xai-grok-cli",
                "x-grok-cli-version": client_version,
                "x-grok-client-version": client_version,
                "x-grok-client-surface": "grok-cli",
                "x-grok-client-identifier": "grok-account-manager",
                "User-Agent": f"grok-cli/{client_version}",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"[错误] billing API 请求失败: {e}")
        return None


def _fetch_user(access_token: str, client_version: str, timeout: int) -> dict | None:
    """获取用户信息和订阅。"""
    try:
        response = requests.get(
            CLI_USER_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "x-xai-token-auth": "xai-grok-cli",
                "x-grok-cli-version": client_version,
                "x-grok-client-version": client_version,
                "x-grok-client-surface": "grok-cli",
                "x-grok-client-identifier": "grok-account-manager",
                "User-Agent": f"grok-cli/{client_version}",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"[错误] user API 请求失败: {e}")
        return None


def _fetch_subscriptions(
    access_token: str,
    user_id: str | None,
    client_version: str,
    timeout: int
) -> dict | None:
    """获取订阅详情。"""
    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json,text/plain,*/*",
            "x-xai-token-auth": "xai-grok-cli",
            "x-grok-cli-version": client_version,
            "x-grok-client-version": client_version,
            "x-grok-client-surface": "grok-cli",
            "x-grok-client-identifier": "grok-account-manager",
            "User-Agent": f"grok-cli/{client_version}",
        }
        if user_id:
            headers["x-userid"] = user_id

        response = requests.get(
            SUBSCRIPTIONS_URL,
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"[错误] subscriptions API 请求失败: {e}")
        return None


def _fetch_task_usage(access_token: str, timeout: int) -> dict | None:
    """获取任务使用情况。"""
    try:
        response = requests.get(
            TASK_USAGE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "x-xai-token-auth": "xai-grok-cli",
                "User-Agent": "Grok Build",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"[错误] task usage API 请求失败: {e}")
        return None


def _extract_quota_from_billing(credential: GrokCredential, billing_data: dict) -> None:
    """从 billing 响应中提取配额信息。"""
    if not billing_data:
        return

    quota = {}

    # 从 config 中提取
    config = billing_data.get("config") or billing_data

    # 周期信息
    current_period = config.get("currentPeriod", {})
    if current_period:
        quota["periodType"] = current_period.get("type")
        quota["periodStart"] = current_period.get("start")
        quota["periodEnd"] = current_period.get("end")

    # On-demand 配额
    if "onDemandUsed" in config:
        on_demand_used = config["onDemandUsed"]
        if isinstance(on_demand_used, dict) and "val" in on_demand_used:
            quota["onDemandUsed"] = float(on_demand_used["val"])
        else:
            quota["onDemandUsed"] = float(on_demand_used)

    if "onDemandCap" in config:
        on_demand_cap = config["onDemandCap"]
        if isinstance(on_demand_cap, dict) and "val" in on_demand_cap:
            quota["onDemandCap"] = float(on_demand_cap["val"])
        else:
            quota["onDemandCap"] = float(on_demand_cap)

    # Prepaid balance
    if "prepaidBalance" in config:
        prepaid = config["prepaidBalance"]
        if isinstance(prepaid, dict) and "val" in prepaid:
            quota["prepaidBalance"] = float(prepaid["val"])
        else:
            quota["prepaidBalance"] = float(prepaid)

    # 兼容旧格式
    if "weeklyLimitPercent" in billing_data:
        quota["weeklyLimitPercent"] = billing_data["weeklyLimitPercent"]
    if "weeklyUsed" in billing_data:
        quota["weeklyUsed"] = billing_data["weeklyUsed"]
    if "weeklyTotal" in billing_data:
        quota["weeklyTotal"] = billing_data["weeklyTotal"]

    # 产品使用情况
    products = config.get("productUsage") or billing_data.get("products")
    if isinstance(products, list):
        quota["products"] = products

    if quota:
        quota.setdefault("products", [])
        credential["quota"] = quota


def _extract_user_info(credential: GrokCredential, user_data: dict) -> None:
    """从 user 响应中提取用户信息。"""
    if not user_data:
        return

    # 用户身份信息
    _set_text_if_present(credential, "user_id", user_data.get("userId"))
    _set_text_if_present(credential, "principal_id", user_data.get("principalId"))
    _set_text_if_present(credential, "principal_type", user_data.get("principalType"))
    _set_text_if_present(credential, "team_id", user_data.get("teamId"))

    # 个人资料（如果注册时没有，从 API 获取）
    if not credential.get("first_name"):
        _set_text_if_present(credential, "first_name", user_data.get("firstName"))
    if not credential.get("last_name"):
        _set_text_if_present(credential, "last_name", user_data.get("lastName"))
    _set_text_if_present(credential, "profile_image_asset_id", user_data.get("profileImageAssetId"))

    # Grok Code 访问权限
    if "hasGrokCodeAccess" in user_data:
        credential["has_grok_code_access"] = user_data["hasGrokCodeAccess"]

    _set_bool_if_present(
        credential,
        "coding_data_retention_opt_out",
        user_data.get("codingDataRetentionOptOut"),
    )

    _set_text_if_present(credential, "subscription_tier", user_data.get("subscriptionTier"))

    # 订阅信息（如果包含在 user 响应中）
    if "subscription" in user_data:
        subscription = user_data["subscription"]
        if isinstance(subscription, dict):
            _set_text_if_present(credential, "subscription_tier", subscription.get("tier"))
            _set_text_if_present(credential, "subscription_status", subscription.get("status"))


def _extract_subscription_info(credential: GrokCredential, subscription_data: dict) -> None:
    """从 subscriptions 响应中提取订阅信息。"""
    if not subscription_data:
        return

    # subscriptions 响应格式: {"subscriptions": [...]}
    subscriptions = subscription_data.get("subscriptions", [])
    if subscriptions and isinstance(subscriptions, list) and len(subscriptions) > 0:
        sub = subscriptions[0]  # 取第一个订阅

        if "tier" in sub:
            credential["subscription_tier"] = sub["tier"]
            # 同时设置 plan_type（如果还没有）
            if not credential.get("plan_type"):
                credential["plan_type"] = sub["tier"]

        if "status" in sub:
            credential["subscription_status"] = sub["status"]

        # 更新 quota 中的订阅信息
        if credential.get("quota"):
            credential["quota"]["subscriptionTier"] = sub.get("tier")
            credential["quota"]["subscriptionStatus"] = sub.get("status")


def _extract_task_usage(credential: GrokCredential, task_usage_data: dict) -> None:
    """从 task usage 响应中提取任务使用情况到配额中。"""
    if not task_usage_data:
        return

    if not isinstance(credential.get("quota"), dict):
        credential["quota"] = {}

    quota = credential["quota"]

    if "frequentUsage" in task_usage_data:
        quota["frequentUsage"] = task_usage_data["frequentUsage"]
    if "frequentLimit" in task_usage_data:
        quota["frequentLimit"] = task_usage_data["frequentLimit"]
    if "occasionalUsage" in task_usage_data:
        quota["occasionalUsage"] = task_usage_data["occasionalUsage"]
    if "occasionalLimit" in task_usage_data:
        quota["occasionalLimit"] = task_usage_data["occasionalLimit"]
    quota.setdefault("products", [])
