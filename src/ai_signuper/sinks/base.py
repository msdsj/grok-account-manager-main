"""Sink 抽象：消费 (provider_name, RegistrationResult) 并把它送到下游
（本地文件、Sub2API、未来其他 dispatcher）。"""

from __future__ import annotations

from typing import Protocol

from ..providers.base import RegistrationResult


class Sink(Protocol):
    def push(self, provider_name: str, result: RegistrationResult) -> None: ...

    def flush(self) -> None: ...
