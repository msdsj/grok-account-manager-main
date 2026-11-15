"""Sink that writes registration results to the project database only."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..providers.base import RegistrationResult


class DatabaseSink:
    def push(self, provider_name: str, result: RegistrationResult) -> None:
        from ..api.services.accounts import persist_registration_result

        persist_registration_result(provider_name, result)

    def flush(self) -> None:
        """Database writes are committed per account; nothing to flush."""
