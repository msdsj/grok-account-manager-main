# Grok OAuth Device Flow

Grok 注册完成后默认会用浏览器中的 `sso` cookie 生成 GrokAccount JSON。需要
`refresh_token / id_token` 时，可以显式开启 OAuth 交换：

```bash
uv run grok-account-manager grok --count 1 --sink json --oauth-exchange
```

## 相关模块

- `serve/grok_account_manager/cli.py`：读取 `--oauth-exchange`。
- `serve/grok_account_manager/providers/grok.py`：完成注册并决定是否进入 OAuth。
- `serve/grok_account_manager/grok/oauth_exchange.py`：申请 device code；优先用注册得到的同一 `sso` 会话直接完成 `verify/approve`，失败时回退到同一个浏览器登录态，并轮询 token endpoint。
- `serve/grok_account_manager/grok/client.py`：整理 GrokAccount JSON。
- `serve/grok_account_manager/sinks/json_credential.py`：写入最终 JSON。

## 注意

该流程不监听固定的本地 loopback callback 端口。注册成功后如果日志出现
`已通过同一 sso 会话直接完成 Build 授权`，说明已跳过 Build 页面等待；如果出现
`回退浏览器自动化`，程序会自动填写设备码并用真实鼠标事件点击 Build/Allow，页面
加载或 Cloudflare 验证仍可能需要人工处理。不要把 SSO/OAuth token 写入日志或提交到 Git。
