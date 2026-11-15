"""Request models used by the local API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    total: int = 1
    concurrency: int = 1
    provider: str = "grok"  # "grok" or "chatgpt"
    oauthExchange: bool = True
    autoImportSub2Api: bool = False
    minimizeBrowsers: bool = True
    emailSource: str = "duckmail"
    outlookData: str = ""
    outlookAccountsFile: str = ""
    googleData: str = ""
    googleAccountsFile: str = ""
    duckMailApiBase: str = Field(default="", max_length=4_096)
    duckMailApiKey: str = Field(default="", max_length=16_384)
    duckMailDomain: str = Field(default="", max_length=1_024)
    cloudMailApiBase: str = Field(default="", max_length=4_096)
    cloudMailAuthMode: str = Field(default="", max_length=32)
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


class ImportAccountsRequest(BaseModel):
    provider: str = ""
    data: str = Field(default="", max_length=5_000_000)


class OutlookMailboxPoolRequest(BaseModel):
    data: str = Field(default="", max_length=1_000_000)


class AccountTagsRequest(BaseModel):
    exportKeys: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    mode: str = Field(default="replace", max_length=16)


class MailConfigRequest(BaseModel):
    emailSource: str = ""
    duckMailApiBase: str = Field(default="", max_length=4_096)
    duckMailApiKey: str | None = Field(default=None, max_length=16_384)
    duckMailDomain: str = Field(default="", max_length=1_024)
    cloudMailApiBase: str = Field(default="", max_length=4_096)
    cloudMailDomains: str = Field(default="", max_length=100_000)
    cloudMailAuthMode: str = Field(default="login", max_length=32)
    cloudMailPublicToken: str | None = Field(default=None, max_length=16_384)
    cloudMailLoginEmail: str = Field(default="", max_length=320)
    cloudMailLoginPassword: str | None = Field(default=None, max_length=4_096)


class DeleteAccountsRequest(BaseModel):
    exportKeys: list[str] = Field(default_factory=list)


class RefreshQuotaRequest(BaseModel):
    accountId: str = ""


class TestBatchRequest(BaseModel):
    exportKeys: list[str] = Field(default_factory=list)
    timeout: int = 180


class AccountChatTestRequest(BaseModel):
    exportKey: str = ""
    model: str = ""
    messages: list[dict[str, Any]] = Field(default_factory=list)
    timeout: int = 180


class AccountImageTestRequest(BaseModel):
    exportKey: str = ""
    model: str = ""
    prompt: str = ""
    n: int = 1
    size: str = "1024x1024"
    timeout: int = 180


class AccountVideoTestRequest(BaseModel):
    exportKey: str = ""
    model: str = ""
    prompt: str = ""
    duration: int = 6
    resolution: str = "720p"
    aspectRatio: str = "16:9"
    timeout: int = 180


class AccountVideoStatusRequest(BaseModel):
    exportKey: str = ""
    videoId: str = ""
    timeout: int = 180


class RelayConfigRequest(BaseModel):
    host: str | None = None
    port: int | None = None
    apiKey: str | None = None
    adminKey: str | None = None
    gatewayProxy: str | None = None

    def to_patch(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class RelaySyncRequest(BaseModel):
    exportKeys: list[str] = Field(default_factory=list)


class RelayModelsRequest(BaseModel):
    probeChat: bool = True
