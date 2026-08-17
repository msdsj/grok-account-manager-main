"""Request models used by the local API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    total: int = 1
    concurrency: int = 1
    oauthExchange: bool = True
    autoImportSub2Api: bool = False
    minimizeBrowsers: bool = True
    emailSource: str = "duckmail"
    outlookData: str = ""
    outlookAccountsFile: str = ""
    googleData: str = ""
    googleAccountsFile: str = ""
    cloudMailApiBase: str = Field(default="", max_length=4_096)
    cloudMailPublicToken: str = Field(default="", max_length=16_384)
    cloudMailLoginEmail: str = Field(default="", max_length=320)
    cloudMailLoginPassword: str = Field(default="", max_length=4_096)
    cloudMailDomains: str = Field(default="", max_length=100_000)
    # ``None`` distinguishes an omitted field (auto-detect the configured
    # default file) from an explicit ``False`` (force direct connection).
    proxyPoolEnabled: bool | None = None
    proxyData: str = Field(default="", max_length=1_000_000)
    proxyFile: str = Field(default="", max_length=4_096)


class RegistrationProxyImportRequest(BaseModel):
    data: str = Field(default="", max_length=1_000_000)
    replace: bool = False


class ExportRequest(BaseModel):
    exportKeys: list[str] = Field(default_factory=list)


class OutlookMailboxPoolRequest(BaseModel):
    data: str = Field(default="", max_length=1_000_000)


class DeleteAccountsRequest(BaseModel):
    exportKeys: list[str] = Field(default_factory=list)


class RefreshQuotaRequest(BaseModel):
    accountId: str = ""


class TestBatchRequest(BaseModel):
    exportKeys: list[str] = Field(default_factory=list)
    timeout: int = 180


class AccountChatTestRequest(BaseModel):
    exportKey: str = ""
    model: str = "grok-4.5"
    messages: list[dict[str, Any]] = Field(default_factory=list)
    timeout: int = 180


class AccountImageTestRequest(BaseModel):
    exportKey: str = ""
    model: str = "grok-imagine-image"
    prompt: str = ""
    n: int = 1
    size: str = "1024x1024"
    timeout: int = 180


class RelayConfigRequest(BaseModel):
    host: str | None = None
    port: int | None = None
    apiKey: str | None = None
    adminKey: str | None = None

    def to_patch(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class RelaySyncRequest(BaseModel):
    exportKeys: list[str] = Field(default_factory=list)


class RelayModelsRequest(BaseModel):
    probeChat: bool = True
