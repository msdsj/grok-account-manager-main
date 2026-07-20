# DuckMail 邮箱源

DuckMail 是默认邮箱源，用于自动创建临时邮箱并轮询 Grok 验证码。

## 配置

复制配置模板：

```bash
cp .env.example .env
```

填写：

```bash
DUCKMAIL_BASE_URL=https://api.duckmail.sbs
DUCKMAIL_API_KEY=your_duckmail_api_key
DUCKMAIL_DOMAIN=@msdsj.cyou
```

## 运行

```bash
uv run grok-account-manager grok --count 1 --sink json
```

DuckMail 实现位于 `src/grok_account_manager/mail/duckmail.py`，邮箱源装配位于
`src/grok_account_manager/mail/sources.py`。

## 安全

`DUCKMAIL_API_KEY` 只放在本地 `.env`，不要提交到 Git。运行产物会写入 `output/`，
该目录默认不进入仓库。
