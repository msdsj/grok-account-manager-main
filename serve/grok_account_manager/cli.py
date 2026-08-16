"""CLI  入口：`uv run grok-account-manager grok --count 1 --sink json`"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from .mail.sources import build_mailbox_source
from .providers.base import Provider
from .providers.grok import GrokProvider
from .core.browser import (
    DrissionBrowserSession,
    build_chromium_options,
    ensure_stable_python_runtime,
    warn_runtime_compatibility,
)
from .core.proxy_pool import (
    ProxyPool,
    ProxyPoolError,
    ProxyPoolExhaustedError,
    get_fixed_egress_proxy,
    load_proxy_file,
    mask_proxy_server,
)
from .api.services.registration_proxy_pool import load_saved_registration_proxies
from .sinks.base import Sink
from .sinks.sub2api import Sub2ApiSink, parse_sub2api_group_ids
from .sinks.txt_file import TxtFileSink
from .sinks.json_credential import JsonCredentialSink

PROVIDERS: dict[str, type[Provider]] = {
    "grok": GrokProvider,
}


class MultiSink:
    """组合多个 sink，按顺序执行 push 和 flush。"""

    def __init__(self, sinks: list[Sink]):
        self.sinks = sinks

    def push(self, provider_name: str, result) -> None:
        for sink in self.sinks:
            try:
                sink.push(provider_name, result)
            except Exception as e:
                print(f"[警告] Sink {sink.__class__.__name__} push 失败: {e}")

    def flush(self) -> None:
        for sink in self.sinks:
            try:
                sink.flush()
            except Exception as e:
                print(f"[警告] Sink {sink.__class__.__name__} flush 失败: {e}")


def _make_sink(args: argparse.Namespace) -> Sink:
    sink_types = args.sink.split("+")  # 支持组合 sink，如 "json+txt"
    sinks = []

    for sink_type in sink_types:
        sink_type = sink_type.strip()

        if sink_type == "txt":
            sinks.append(TxtFileSink(args.output))

        elif sink_type == "json":
            sinks.append(JsonCredentialSink(args.json_output))

        elif sink_type == "sub2api":
            base_url = os.environ.get("SUB2API_BASE_URL", "")
            api_key = os.environ.get("SUB2API_ADMIN_API_KEY", "")
            groups_raw = os.environ.get("SUB2API_DEFAULT_GROUP_IDS", "")
            group_ids = parse_sub2api_group_ids(groups_raw)
            sinks.append(
                Sub2ApiSink(
                    base_url=base_url,
                    api_key=api_key,
                    default_group_ids=group_ids,
                    batch_size=args.batch_size,
                )
            )

        else:
            raise ValueError(f"unknown sink: {sink_type}")

    if len(sinks) == 1:
        return sinks[0]
    return MultiSink(sinks)


def _build_cli_proxy_pool(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> tuple[ProxyPool | None, str]:
    """Load the optional CLI proxy pool without logging raw endpoint values."""
    if args.no_proxy:
        return None, ""

    configured_proxy_file = str(args.proxy_file or "").strip()
    if configured_proxy_file:
        candidate = Path(configured_proxy_file).expanduser()
        if not candidate.is_file():
            parser.error(f"代理池文件不存在或不是普通文件：{candidate}")
        try:
            pool = ProxyPool.from_file(candidate)
        except (OSError, UnicodeError, ProxyPoolError) as error:
            parser.error(f"代理池加载失败：{error}")
        source = "指定文件"
    else:
        default_file = Path.home() / "Downloads" / "xx.txt"
        try:
            file_values = load_proxy_file(default_file) if default_file.is_file() else ()
            saved_values = load_saved_registration_proxies()
            pool = ProxyPool.from_lines((*file_values, *saved_values))
        except (OSError, RuntimeError, UnicodeError, ProxyPoolError) as error:
            parser.error(f"代理池加载失败：{error}")

        if file_values and saved_values:
            source = "默认文件和已保存节点"
        elif file_values:
            source = "默认文件"
        elif saved_values:
            source = "已保存节点"
        else:
            return None, ""

    if pool.total <= 0:
        parser.error("代理池没有可用地址")
    return pool, source


def main() -> None:
    ensure_stable_python_runtime()
    warn_runtime_compatibility()
    load_dotenv()

    parser = argparse.ArgumentParser(description="MSDSJ Grok 账号注册与凭证管理器")
    parser.add_argument("provider", choices=list(PROVIDERS), help="目标 AI 服务（如 grok）")
    parser.add_argument("--count", type=int, default=0, help="执行轮数，0 表示无限循环")
    parser.add_argument(
        "--sink",
        default="txt",
        help='产物下游，支持: txt, json, sub2api，可组合使用如 "json+txt"',
    )
    parser.add_argument("--output", default="output/sso.txt", help="txt sink 输出文件")
    parser.add_argument(
        "--json-output",
        default="output/credentials",
        help="json sink 输出目录（每个凭证保存为独立 JSON 文件）",
    )
    parser.add_argument("--batch-size", type=int, default=1, help="sub2api sink 批次大小")
    parser.add_argument(
        "--oauth-exchange",
        action="store_true",
        help="注册后尝试 xAI OAuth Device Flow 换取 refresh_token；需要网页授权，默认关闭以避免卡住注册",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="后台运行（无浏览器窗口），注意：Turnstile 可能检测并拒绝 headless 模式",
    )
    parser.add_argument(
        "--proxy-file",
        default=os.environ.get("GROK_ACCOUNT_MANAGER_PROXY_FILE", ""),
        help="代理池文件（每行 HOST:PORT；留空时合并 ~/Downloads/xx.txt 与已保存节点）",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="强制直连，忽略代理池文件",
    )
    parser.add_argument(
        "--email-source",
        choices=["duckmail", "outlook", "gmail", "google", "cloud_mail"],
        default=os.environ.get("GROK_ACCOUNT_MANAGER_EMAIL_SOURCE", "duckmail"),
        help="邮箱来源：duckmail、outlook、gmail、google 或 cloud_mail；google 表示走 Google 账号注册按钮",
    )
    parser.add_argument(
        "--outlook-accounts-file",
        default=os.environ.get("OUTLOOK_ACCOUNTS_FILE", ""),
        help="Outlook 账号文件路径，每行格式为 邮箱----密码----clientId----refreshToken",
    )
    parser.add_argument(
        "--outlook-accounts",
        default=os.environ.get("OUTLOOK_ACCOUNTS", ""),
        help="Outlook 账号池原文，支持多行，格式为 邮箱----密码----clientId----refreshToken",
    )
    parser.add_argument(
        "--google-accounts-file",
        default=os.environ.get("GOOGLE_ACCOUNTS_FILE", ""),
        help="Google 账号文件路径，每行格式为 邮箱----密码----辅助邮箱(可选)",
    )
    parser.add_argument(
        "--google-accounts",
        default=os.environ.get("GOOGLE_ACCOUNTS", ""),
        help="Google 账号池原文，支持多行；gmail 模式建议填 邮箱----应用专用密码",
    )
    parser.add_argument(
        "--cloud-mail-api-base",
        default=os.environ.get("CLOUD_MAIL_API_BASE", ""),
        help="Cloud Mail 站点地址，例如 https://mail.example.com",
    )
    parser.add_argument(
        "--cloud-mail-public-token",
        default=os.environ.get("CLOUD_MAIL_PUBLIC_TOKEN", ""),
        help="Cloud Mail Public Token；未配置时使用登录邮箱和密码",
    )
    parser.add_argument(
        "--cloud-mail-login-email",
        default=os.environ.get("CLOUD_MAIL_LOGIN_EMAIL", ""),
        help="Cloud Mail 登录邮箱",
    )
    parser.add_argument(
        "--cloud-mail-login-password",
        default=os.environ.get("CLOUD_MAIL_LOGIN_PASSWORD", ""),
        help="Cloud Mail 登录密码",
    )
    parser.add_argument(
        "--cloud-mail-domains",
        default=os.environ.get("CLOUD_MAIL_DOMAINS", ""),
        help="Cloud Mail 邮箱域名，多个域名使用逗号或换行分隔",
    )
    args = parser.parse_args()

    proxy_pool, proxy_source = _build_cli_proxy_pool(args, parser)
    if proxy_pool is not None:
        print(
            f"[*] 已加载代理池 {proxy_pool.total} 个端点（来源：{proxy_source}）；"
            "每轮随机且任务内不复用"
        )

    target_count = args.count
    if proxy_pool is not None and target_count <= 0:
        # The old zero-count mode is infinite. A finite pool cannot satisfy
        # that contract without reusing an endpoint, so it ends at exhaustion.
        target_count = proxy_pool.total
        print(f"[*] 检测到有限代理池，无限轮次将执行 {target_count} 轮后停止")
    elif proxy_pool is not None and target_count > proxy_pool.total:
        print(
            f"[警告] 请求 {target_count} 轮但代理池只有 {proxy_pool.total} 个端点，"
            f"已限制为 {proxy_pool.total} 轮以保持出口不复用"
        )
        target_count = proxy_pool.total

    provider_cls = PROVIDERS[args.provider]
    provider = provider_cls()
    if hasattr(provider, "enable_oauth_exchange"):
        provider.enable_oauth_exchange = args.oauth_exchange
    provider.mail_source = build_mailbox_source(
        email_source=args.email_source,
        outlook_data=args.outlook_accounts,
        outlook_file=args.outlook_accounts_file,
        google_data=args.google_accounts,
        google_file=args.google_accounts_file,
        cloud_mail_api_base=args.cloud_mail_api_base,
        cloud_mail_public_token=args.cloud_mail_public_token,
        cloud_mail_login_email=args.cloud_mail_login_email,
        cloud_mail_login_password=args.cloud_mail_login_password,
        cloud_mail_domains=args.cloud_mail_domains,
    )
    sink = _make_sink(args)

    initial_proxy = proxy_pool.acquire() if proxy_pool is not None else get_fixed_egress_proxy()
    if initial_proxy:
        print(f"[*] 首轮使用代理 {mask_proxy_server(initial_proxy)}")

    fixed_egress_proxy = get_fixed_egress_proxy()
    if proxy_pool is not None or fixed_egress_proxy:
        def restart_with_fresh_proxy(active_session: DrissionBrowserSession) -> None:
            retry_proxy = proxy_pool.acquire() if proxy_pool is not None else fixed_egress_proxy
            print(f"[*] 本轮重试使用代理 {mask_proxy_server(retry_proxy)}")
            active_session.restart(proxy_url=retry_proxy)

        provider.retry_browser_callback = restart_with_fresh_proxy

    browser_options = {"headless": args.headless}
    if initial_proxy is not None:
        browser_options["proxy_url"] = initial_proxy
    session = DrissionBrowserSession(
        build_chromium_options(provider.chrome_lang, **browser_options)
    )
    session.start()

    rounds_done = 0
    try:
        while True:
            if target_count > 0 and rounds_done >= target_count:
                break
            rounds_done += 1
            print(f"\n[*] 开始第 {rounds_done} 轮注册（provider={provider.name}）")
            try:
                result = provider.run_round(session)
                if args.oauth_exchange:
                    full_credential = result.get("full_credential")
                    has_refresh_token = isinstance(full_credential, dict) and bool(
                        str(full_credential.get("refresh_token") or "").strip()
                    )
                    if result.get("oauth_status") != "ready" or not has_refresh_token:
                        print("[Error] 本轮未获取 refresh_token，按配置不保存 SSO 或 JSON 凭证")
                        continue
                sink.push(provider.name, result)
                print(f"[*] 本轮注册完成，邮箱: {result['email']}")
            except KeyboardInterrupt:
                print("\n[Info] 收到中断信号，停止后续轮次。")
                break
            except Exception as error:
                print(f"[Error] 第 {rounds_done} 轮失败: {error}")
            finally:
                is_last = target_count > 0 and rounds_done >= target_count
                if not is_last:
                    try:
                        next_proxy = proxy_pool.acquire() if proxy_pool is not None else fixed_egress_proxy
                        if next_proxy:
                            print(f"[*] 下一轮使用代理 {mask_proxy_server(next_proxy)}")
                        if proxy_pool is not None or fixed_egress_proxy:
                            session.restart(proxy_url=next_proxy)
                        else:
                            session.restart()
                    except ProxyPoolExhaustedError:
                        print("[Error] 代理池已耗尽，停止后续轮次")
                        target_count = rounds_done
                    except Exception as error:
                        print(f"[Error] 下一轮浏览器重启失败：{error}")
                        target_count = rounds_done

            if target_count == 0 or rounds_done < target_count:
                time.sleep(2)

        sink.flush()
    finally:
        session.stop()


if __name__ == "__main__":
    main()
