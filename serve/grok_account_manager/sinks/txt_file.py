"""把凭证按行追加到文本文件的  sink。每行一条凭证（如 sso JWT），无结构。"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from ..providers.base import RegistrationResult


_TXT_WRITE_LOCK = threading.Lock()


class TxtFileSink:
    def __init__(self, output_path: str | os.PathLike[str]):
        self.output_path = Path(output_path)

    def push(self, provider_name: str, result: RegistrationResult) -> None:
        if str(result.get("oauth_status") or "not_requested") in {"pending", "failed"}:
            raise ValueError("refresh_token 尚未成功获取，拒绝写入 SSO 文本")
        credential = (result.get("credential") or "").strip()
        if not credential:
            raise Exception("待写入的凭证为空")

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with _TXT_WRITE_LOCK:
            file_descriptor = os.open(
                self.output_path,
                os.O_RDWR | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            try:
                self.output_path.chmod(0o600)
                with os.fdopen(file_descriptor, "a+", encoding="utf-8") as handle:
                    file_descriptor = -1
                    handle.seek(0)
                    if any(line.rstrip("\r\n") == credential for line in handle):
                        print(f"[*] 凭证已存在，跳过重复追加: {self.output_path}")
                        return
                    handle.write(credential + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                if file_descriptor >= 0:
                    os.close(file_descriptor)
        print(f"[*] 已追加凭证到文件: {self.output_path}")

    def flush(self) -> None:
        # 文本 sink 每次 push 立即落盘，flush 是空操作。
        return
