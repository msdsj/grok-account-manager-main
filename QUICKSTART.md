# 快速启动

## 1. 准备一次环境

需要 Python 3.12/3.13、[uv](https://docs.astral.sh/uv/)、Node.js 20+（pnpm 或 npm 均可）。

```bash
# macOS / Linux，安装 uv（已安装可跳过）
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 2. 一条命令启动

```bash
./scripts/start.sh
```

脚本会自动完成：生成 `.env`（首次）、同步 Python 依赖、安装并构建前端、启动 FastAPI，然后打开浏览器。

打开 <http://127.0.0.1:43187>，默认账号 `admin`，默认密码 `admin`。首次登录后可在左下角修改密码。

> 注册、账号管理、Grok / GPT / Kiro 三网关、请求审计和前端都在同一条 Python 进程内，不需要 Docker、Go 网关或额外启动 Vite。

## 3. 配置邮箱与代理

进入控制台“设置”：

- 邮箱来源：DuckMail、Cloud Mail、Outlook、Gmail/Google
- DuckMail / Cloud Mail 的地址、域名与密钥
- 注册代理池：按行粘贴 `HOST:PORT` 或 `http(s)://...`

保存后到“注册机”页面启动注册任务。

## 4. 调用内置网关

在“密钥”页面创建密钥（不创建时本机直连放行）：

```bash
curl http://127.0.0.1:43187/v1/models

curl http://127.0.0.1:43187/v1/chat/completions \
  -H "Authorization: Bearer gak_xxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-4.20-auto","messages":[{"role":"user","content":"你好"}]}'

curl http://127.0.0.1:43187/v1/images/generations \
  -H "Authorization: Bearer gak_xxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-image-1","prompt":"a cat"}'
```

模型与 provider 的对应关系在“模型路由”页面查看，调用结果在“请求审计”页面查看。

## 5. 更新与自检

```bash
./scripts/update.sh   # 拉取代码、同步依赖、重建前端
./scripts/verify.sh   # 编译检查、单元测试、前端构建、启动冒烟
```

## 常见问题

- 端口被占用：`uv run grok-account-manager-api --port 43190`
- 前端页面空白：执行 `./scripts/verify.sh`，确认 `web/dist` 已生成
- 注册浏览器不弹出：确认本机安装了 Chrome/Chromium，且没有被其他进程占用
- 想看运行状态：<http://127.0.0.1:43187/readyz>
