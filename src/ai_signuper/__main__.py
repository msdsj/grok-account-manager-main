"""CLI 入口：`uv run python -m ai_signuper grok --count 1 --sink txt`"""

from __future__ import annotations

import argparse
import os
import time

from dotenv import load_dotenv

from .providers.base import Provider
from .providers.grok import GrokProvider
from .runtime import (
    DrissionBrowserSession,
    build_chromium_options,
    ensure_stable_python_runtime,
    warn_runtime_compatibility,
)
from .sinks.base import Sink
from .sinks.sub2api import Sub2ApiSink
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
            group_ids = [int(x) for x in groups_raw.split(",") if x.strip()]
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


def main() -> None:
    ensure_stable_python_runtime()
    warn_runtime_compatibility()
    load_dotenv()

    parser = argparse.ArgumentParser(description="AI 服务自动注册机")
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
        help="注册后尝试 xAI OAuth PKCE loopback 换取 refresh_token；需要网页授权，默认关闭以避免卡住注册",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="后台运行（无浏览器窗口），注意：Turnstile 可能检测并拒绝 headless 模式",
    )
    args = parser.parse_args()

    provider_cls = PROVIDERS[args.provider]
    provider = provider_cls()
    if hasattr(provider, "enable_oauth_exchange"):
        provider.enable_oauth_exchange = args.oauth_exchange
    sink = _make_sink(args)

    session = DrissionBrowserSession(
        build_chromium_options(provider.chrome_lang, headless=args.headless)
    )
    session.start()

    rounds_done = 0
    try:
        while True:
            if args.count > 0 and rounds_done >= args.count:
                break
            rounds_done += 1
            print(f"\n[*] 开始第 {rounds_done} 轮注册（provider={provider.name}）")
            try:
                result = provider.run_round(session)
                sink.push(provider.name, result)
                print(f"[*] 本轮注册完成，邮箱: {result['email']}")
            except KeyboardInterrupt:
                print("\n[Info] 收到中断信号，停止后续轮次。")
                break
            except Exception as error:
                print(f"[Error] 第 {rounds_done} 轮失败: {error}")
            finally:
                is_last = args.count > 0 and rounds_done >= args.count
                if not is_last:
                    session.restart()

            if args.count == 0 or rounds_done < args.count:
                time.sleep(2)

        sink.flush()
    finally:
        session.stop()


if __name__ == "__main__":
    main()
