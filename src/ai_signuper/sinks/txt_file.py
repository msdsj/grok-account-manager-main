"""把凭证按行追加到文本文件的 sink。每行一条凭证（如 sso JWT），无结构。"""

from __future__ import annotations

import os
from pathlib import Path

from ..providers.base import RegistrationResult


class TxtFileSink:
    def __init__(self, output_path: str | os.PathLike[str]):
        self.output_path = Path(output_path)

    def push(self, provider_name: str, result: RegistrationResult) -> None:
        credential = (result.get("credential") or "").strip()
        if not credential:
            raise Exception("待写入的凭证为空")

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("a", encoding="utf-8") as f:
            f.write(credential + "\n")
        print(f"[*] 已追加凭证到文件: {self.output_path}")

    def flush(self) -> None:
        # 文本 sink 每次 push 立即落盘，flush 是空操作。
        return
