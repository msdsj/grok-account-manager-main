#  Grok OAuth Flow

Grok 注册完成后默认会用浏览器中的 `sso` cookie 生成 GrokAccount JSON。需要
`refresh_token / id_token` 时，可以显式开启 OAuth 交换：

```bash
uv run grok-account-manager grok --count 1 --sink json --oauth-exchange
```

## 相关模块

- `src/grok_account_manager/cli.py`：读取 `--oauth-exchange`。
- `src/grok_account_manager/providers/grok.py`：完成注册并决定是否进入 OAuth。
- `src/grok_account_manager/grok/oauth_exchange.py`：执行 Authorization Code + PKCE loopback。
- `src/grok_account_manager/grok/client.py`：整理 GrokAccount JSON。
- `src/grok_account_manager/sinks/json_credential.py`：写入最终 JSON。

## 注意

OAuth loopback 会监听 `127.0.0.1:56121/callback`。Cloudflare 或授权确认页面仍可能
需要人工处理，这部分不要强行改成完全无交互流程。
