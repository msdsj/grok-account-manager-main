# Cloud Mail 邮箱源

本项目支持 maillab/cloud-mail 兼容 API，用于在 Grok 页面注册流程中创建邮箱并读取验证码。注册页面仍由浏览器自动化完成；Cloud Mail 只替换邮箱创建和收信部分。

## 认证方式

Public Token 模式使用：

- `POST /api/public/addUser` 创建邮箱。
- `POST /api/public/emailList` 查询该地址的邮件。

账号登录模式使用：

- `POST /api/login` 获取登录 token。
- `POST /api/account/add` 创建邮箱并取得 `accountId`。
- `GET /api/email/latest` 按 `accountId` 和邮件游标查询新邮件。

Cloud Mail 的 `Authorization` 值是原始 Token，不添加 `Bearer` 前缀。请求必须同时满足 HTTP 状态为 `200` 且 JSON 响应中的 `code` 为 `200`。登录 token 失效并返回 HTTP 401 或业务 `code=401` 时，程序会重新登录一次并重试原请求。

## 配置

Public Token：

```dotenv
GROK_ACCOUNT_MANAGER_EMAIL_SOURCE=cloud_mail
CLOUD_MAIL_API_BASE=https://mail.example.com
CLOUD_MAIL_DOMAINS=example.com,example.net
CLOUD_MAIL_PUBLIC_TOKEN=replace-with-public-token
```

账号登录：

```dotenv
GROK_ACCOUNT_MANAGER_EMAIL_SOURCE=cloud_mail
CLOUD_MAIL_API_BASE=https://mail.example.com/api
CLOUD_MAIL_DOMAINS=example.com
CLOUD_MAIL_LOGIN_EMAIL=admin@example.com
CLOUD_MAIL_LOGIN_PASSWORD=replace-with-password
```

`CLOUD_MAIL_API_BASE` 末尾可以带 `/api`，程序会统一规范化。域名可以带或不带 `@`，多个域名使用逗号或换行分隔；每轮从有效域名中随机选择一个。

控制台用户可以在“注册任务”页面选择 Cloud Mail，并在 Public Token 与账号登录之间切换。只有当前认证方式的凭据会发送给后端。

## 收信与安全

程序只处理目标收件人且 `emailId` 高于当前游标的新邮件，并复用 Grok 邮箱流程的严格验证码提取规则。任务停止时，邮箱轮询会响应停止信号并退出。

TLS 证书验证保持开启。Public Token、登录密码和站点 token 都不会写入任务快照或普通日志，但会在当前进程内保留以支持任务重试。请只把真实凭据放在本机 `.env` 或控制台输入中，不要提交到 Git，也不要将完整日志公开。
