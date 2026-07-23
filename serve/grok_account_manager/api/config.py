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
ROUND_TIMEOUT_SECONDS = 180
