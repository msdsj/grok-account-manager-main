"""Provider  抽象。每个 AI 服务实现一个 Provider，描述 signup URL、所需浏览器语言、
成功后取的 cookie 名，以及一轮完整的注册流程。"""

from __future__ import annotations

from typing import Any, Protocol, TypedDict, runtime_checkable


class RegistrationResult(TypedDict, total=False):
    email: str
    credential: str
    profile: dict[str, str]
    full_credential: dict[str, Any]


class BrowserSession(Protocol):
    """运行期浏览器会话句柄。Provider 通过它访问当前活动 page、重启浏览器等。

    具体实现见 grok_account_manager.core.browser.DrissionBrowserSession。
    """

    @property
    def page(self): ...

    def refresh_page(self): ...

    def open_new_tab(self, url: str | None = None): ...

    def restart(self): ...


@runtime_checkable
class Provider(Protocol):
    name: str
    signup_url: str
    chrome_lang: str
    success_cookie_name: str
    mail_source: Any | None

    def run_round(self, session: BrowserSession) -> RegistrationResult: ...
