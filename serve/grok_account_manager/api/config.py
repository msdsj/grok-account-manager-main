"""Backend path and runtime constants."""

from __future__ import annotations

from pathlib import Path

from ..core.browser import PROJECT_ROOT

OUTPUT_DIR: Path = PROJECT_ROOT / "output"
CREDENTIALS_DIR: Path = OUTPUT_DIR / "credentials"
TXT_OUTPUT: Path = OUTPUT_DIR / "sso.txt"
ACCOUNT_TEST_RESULTS_PATH: Path = OUTPUT_DIR / "account-test-results.json"
WEB_DIST_DIR: Path = PROJECT_ROOT / "web" / "dist"

DEFAULT_MAX_CONCURRENCY = 20
DEFAULT_MAX_OAUTH_CONCURRENCY = 2
DEFAULT_OAUTH_ACCESS_DENIED_CIRCUIT_THRESHOLD = 2
REGISTRATION_ROUND_TIMEOUT_SECONDS = 300
OAUTH_ROUND_TIMEOUT_SECONDS = 1200

# 同一个 worker 完成一轮注册、重启浏览器之后，到抢下一轮之前的随机等待区间（秒）。
# 拉开注册请求的节奏，避免同一来源在短时间内密集发起注册被风控按"过于规律"识别。
ROUND_PACING_MIN_SECONDS = 4.0
ROUND_PACING_MAX_SECONDS = 11.0

# 并发 worker 不在同一毫秒启动浏览器和提交注册请求。第 N 个 worker 会在这个
# 基础区间乘以 N-1 后随机等待，5 并发时最后一个窗口通常晚 4-12 秒启动。
WORKER_START_STAGGER_MIN_SECONDS = 1.0
WORKER_START_STAGGER_MAX_SECONDS = 3.0
