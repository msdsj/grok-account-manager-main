# 全 Python 三网关统一重写实施计划

## Context

项目当前是 FastAPI 管理端 + vendored Go 网关（`gateway/` = grok2api）+ React 控制台的混合体，目录与职责交叉，难以维护。目标重写为：

- **后端全部 Python**：单 FastAPI 进程，统一管理 API、三网关 `/v1`、注册、测试、导入导出
- **前端统一 React**：保留现有设计系统，重构信息架构与关键页面
- **三大网关**：Grok（对话/图片/视频）、GPT（图片）、Kiro（对话/模型）
- **四个核心能力**：注册账号、功能测试、导入导出、网关调用
- **最终不再保留 Go 网关、Docker 网关与 Node sidecar**

已确认决策：全部 Python；分阶段交付；Grok 先迁对话/图片、视频后续；Kiro 网关 Python 重写；Kiro 可视化 = DrissionPage 浏览器自动化；GPT 先做协议导入登录，再实验纯协议新建；平滑迁移旧数据；并发按屏幕容量排队。

> 说明：此前的本地 Kiro 代码已在清理操作中移除，Kiro 将按参考项目重新实现。

## 目标架构

新建干净的 Python 包 `server/`，旧 `serve/grok_account_manager/` 与 `gateway/` 只作迁移期参照，P7 删除。入口保留 `uv run grok-account-manager-api` 兼容。

```
server/
  main.py settings.py
  core/{db.py,migrations.py,security.py,events.py,http.py}
  domain/{accounts.py,quota.py,models.py,selector.py,import_export.py}
  gateways/
    base.py
    grok/{client.py,oauth.py,chat.py,image.py,video.py,quota.py,models.py,media.py}
    gpt/{account.py,image.py,chat.py,oauth.py,sentinel.py,pow.py,turnstile.py,quota.py}
    kiro/{auth.py,api.py,translator.py,pool.py,quota.py,models.py}
  registration/
    base.py runner.py events.py mail.py proxy.py credential.py
    browser/{session.py,geometry.py,actions.py,fingerprint.py}
    providers/grok/{browser.py,normalizer.py}
    providers/chatgpt/{browser.py,oauth_import.py,password_import.py,normalizer.py,protocol_signup.py}
    providers/kiro/{browser.py,protocol.py,normalizer.py}
  api/{gateway.py,accounts.py,quotas.py,register.py,plaza.py,config.py,models.py,keys.py,audits.py,import_export.py,system.py}
  config/{store.py,schemas.py}
```

运行形态：

- 单进程 `127.0.0.1:43187`：管理 API + `/v1` 网关 + React 静态资源
- `43188`：Vite 开发端
- 数据库：`output/app.db`（SQLite，WAL + 0600），凭证文件仅作导出物
- 不再需要 43871 Go 网关端口与 Docker 容器

## 统一数据模型

- `accounts(id, provider, email, display_name, auth_mode, plan, status, capabilities_json, models_json, proxy, created_at, updated_at, last_checked_at, deleted_at)`
- `credentials(account_id PK, provider, secret_json, expires_at, refresh_at)`
- `quota_snapshots(id, account_id, capability, metric, used, total, remaining, unit, reset_at, raw_json, captured_at)`
- `model_catalog(public_model PK, provider, upstream_model, capability, enabled, weight, config_json)`
- `provider_configs(section PK, json, updated_at)`
- `jobs` / `job_events` / `test_runs` / `test_results` / `client_keys` / `audit_logs` / `import_history`

DTO：

- `Account{id, provider, email, displayName, plan, status, capabilities:{chat,image,video,edit}, models, quota, health, createdAt}`
- `QuotaSummary{windows:[{key,label,used,total,remaining,percent,resetAt,unit}], updatedAt, source}`
- `CredentialEnvelope{provider, accountId, authMode, tokens:{access,refresh,id,sso}, expiresAt, extra:{region,profileArn,deviceId}, source}`

迁移：读取旧 SQLite `accounts/account_test_results` 与 `output/credentials/*.json`，按 `export_key` 生成稳定 id；旧表保留 `_legacy_*`；备份 DB 与凭证目录；支持 `--dry-run` 与幂等重跑。

## 统一网关 `/v1`

路由：`/v1/models`、`/v1/chat/completions`、`/v1/responses`、`/v1/messages`、`/v1/images/generations`、`/v1/images/edits`、`/v1/videos/generations`、`/v1/videos/{id}`。

模型路由（`model_catalog`）：

- `grok-*` → `gateways/grok/`：按 grok2api Go 源码逐模块移植（Grok Build / Web / Console 三类上游、OpenAI/Anthropic 协议、图片、视频异步任务）
- `gpt-image-*` → `gateways/gpt/`：移植 chatgpt2api 的图片生成/编辑、sentinel/PoW/Turnstile 与账号 token 刷新
- `kiro-claude-*`、`kiro-auto` → `gateways/kiro/`：移植 CodeWhisperer/Amazon Q 调用、OpenAI/Anthropic 转换、账号池、额度与断路器

统一治理：

- `client_keys` 哈希存储，scope（chat/image/video）+ 模型白名单
- `domain/selector.py`：round-robin / sticky / least-used；401/403 冷却、429 退避、5xx 换号，最多 3 次
- `audit_logs` 记录请求、账号、耗时、上游状态、错误
- 图片/视频资产统一落 `output/media/`，提供下载与画廊 API

## 注册中心

`registration/base.py` 定义 `RegistrationProvider`（`mode in {browser, protocol, import}`、`preflight/run/normalize/validate/refresh`）；`runner.py` 统一并发池、窗口槽位、错峰、超时、取消、事件落库。

- **Grok**：迁移现有 `providers/grok.py` 浏览器流程与 OAuth 交换，normalizer 产出 sso/refresh/plan/quota
- **ChatGPT**：
  - `browser.py`：迁移现有 `providers/chatgpt.py` 自动化注册
  - `oauth_import.py` / `password_import.py`：移植 chatgpt2api 的 PKCE 授权码与密码登录换 token
  - `protocol_signup.py`：P7 实验性纯协议新建，失败自动回退浏览器
- **Kiro**：
  - `protocol.py`：移植参考 `registrar.ts` 的 OIDC client register、device_authorization、portal workflow、OTP、create identity、set password、SSO token、JWE 密码加密
  - `browser.py`：DrissionPage 打开 AWS Builder ID 可视化注册，复用统一 `browser/actions.py`
- **邮箱**：复用现有 DuckMail / Cloud Mail / Outlook / Google（`mail/sources.py`），不引入 Remail；注册页内嵌可视化邮箱配置
- **产出**：统一 `CredentialEnvelope` 入库，同时写 `output/credentials/{grok,chatgpt,kiro}_credentials.json`，保持导出与 Sub2API 工具链兼容

## 账号额度

- Grok：移植 grok2api 的账号额度刷新（免费/付费、Web/Console/Imagine 窗口），写入 `quota_snapshots`
- GPT：chatgpt2api 账号 `quota/status/type` 与用户信息用量
- Kiro：CodeWhisperer `getUsageLimits` 与账号池额度字段
- `domain/quota.py`：并发限流、最小刷新间隔、失败退避、历史快照；API `GET /api/quotas`、`POST /api/quotas/refresh`
- UI：账号页按 provider 过滤，Grok/GPT/Kiro 统一额度卡片与窗口进度条，复用 `web/src/features/accounts/account-quota.tsx`

## 测试广场

- `POST /api/plaza/runs`（provider/capability/model/accountIds/prompt/params/concurrency）复用 JobRunner
- `GET /api/plaza/runs/{id}` 轮询进度与结果
- 前端 `/test-plaza`：Grok 对话/图片/视频、GPT 图片、Kiro 对话分区；实时进度、延迟、错误、图片/视频预览、批量重测
- 每次测试结果写入 `test_results`，供账号健康度与额度页复用

## 导入导出

- 统一导入：`POST /api/import/preview` + `/api/import/commit`，支持 JSON/CSV/TXT，自动识别 provider 与格式，重复账号按 provider+email 检测，冲突策略 skip/overwrite/merge
- 统一导出：`POST /api/export`，按选中账号或 provider 过滤，格式 JSON/CSV，并保留 Sub2API/CPA 导出
- 页面：`/import-export`，导入预览、字段映射、结果统计；导出支持账号选择与格式选择

## 配置中心

`provider_configs` 分节：

- `mail`：邮箱来源、域名、取码轮询与失败策略（复用现有 `mail_config.py`）
- `registration`：并发上限、窗口尺寸档位、启动错峰、超时、代理池
- `gateway`：Grok 上游/代理、GPT 代理与开关、Kiro 端点/区域/代理
- `models`：公共模型 ↔ 上游模型 ↔ provider/能力映射
- `security`：管理员密码、客户端密钥、凭证访问策略

API：`GET/PUT /api/config/{section}`，Pydantic 校验，敏感值打码；`.env` 仅作默认值与兼容。

## 前端信息架构

导航分组：

- 总览：仪表盘
- 资源：账号与额度、模型目录、导入导出
- 生产：注册中心、测试广场
- 网关：创作工作台、图片画廊、视频画廊、请求审计、质量守卫
- 接入：客户端密钥、API 文档
- 系统：配置中心、系统状态

路由新增 `/test-plaza`、`/import-export`、`/config-center`、`/media/images`、`/media/videos`；旧路径重定向。注册中心拆「任务看板 / 流程配置 / 历史」三 Tab。复用 `dashboard-panel.tsx`、`account-quota.tsx`、`register-task/*` 与 UI 组件；i18n 拆分到 `locales/{zh-CN,en-US}/*.json`。

## 多并发浏览器点击可靠性

- `browser/geometry.py`：按屏幕容量生成窗口槽位（最小尺寸、级联偏移、视口/DPR 归一），同时设置 `--window-size` 与 CDP `Emulation.setDeviceMetricsOverride`、`--force-device-scale-factor=1`；超出槽位排队
- `browser/actions.py`：分层点击 —— locator → `scrollIntoView({block:"center"})` + JS 原生事件序列 → CDP `Input.dispatchMouseEvent`（CSS 坐标 × DPR）→ Enter；每步用 `document.elementFromPoint` 校验命中；支持 iframe
- 焦点：关键步骤前 `Page.bringToFront`/`Target.activateTarget`；默认保持窗口可见，不用屏幕坐标点击
- 失败取证：截图 + DOM 快照 + 候选选择器得分写入 `job_events`
- 验证：`tests/test_browser_click_reliability.py` 本地合成页 4 种尺寸 × 10 并发；再做 3/10 并发真实注册压测

## 分阶段计划

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | 新 `server/` 骨架、单进程入口、DB v2 与迁移框架、旧数据备份；Go 网关仅作对照保留 | `compileall`、旧测试通过、迁移 dry-run 幂等 |
| P1 | 统一账号/额度/导入导出；旧数据迁移；账号页与导入导出页 | 旧账号可读、Grok 额度刷新、导入导出闭环 |
| P2 | 统一 `/v1` 骨架、模型目录、密钥/审计/选号；Grok 对话/图片 Python 移植 | curl Grok chat/image 流式与非流式通过 |
| P3 | 注册框架、Grok 浏览器迁移、窗口槽位与点击可靠性 | 3 并发注册、点击可靠性测试通过 |
| P4 | Kiro Python 网关（对话/模型/额度）+ Kiro 协议与可视化注册 | Kiro chat 调通；两种注册各跑通 1 单 |
| P5 | GPT 图片网关、GPT OAuth/密码导入、浏览器注册 | GPT 图片生成/编辑、协议导入成功 |
| P6 | 测试广场 + 配置中心 + 前端 IA 重构；旧 API 重定向 | 广场三 provider 测试、配置读写、`npm run build` |
| P7 | Grok 视频 Python 移植与异步任务；删除 `gateway/` Go 与旧包；全量验收与文档 | 视频生成/查询通过、无 Go/Node 依赖、全链路冒烟 |

每阶段独立提交；P1/P2/P4/P7 前备份 DB 与凭证；Go 网关在 P7 前仅作对照，不再对外暴露。

## 验证方案

- 后端：`uv run python -m compileall server tests`
- 单测：迁移幂等、账号 DTO、额度刷新、网关路由（mock 上游）、选号/重试、点击可靠性、注册 runner 并发
- 前端：`cd web && npm run build`
- 网关矩阵：`/v1/models`、Grok chat/image(/video)、GPT image、Kiro chat，流式与非流式
- 注册：Grok 浏览器 3 并发、ChatGPT 协议导入 + 浏览器、Kiro 协议 + 浏览器
- 导出：JSON/CSV/Sub2API/CPA；导入预览与冲突策略
- 迁移：对比旧新账号数、凭证字段、额度快照；回滚演练

## 风险与约束

- Grok 网关 Go → Python 是本项目最大工作量，P2/P7 分两次交付；先保证对话/图片，再打视频。
- GPT 纯协议新建账号可行性不确定，P7 仅实验，默认保留导入 + 浏览器注册。
- Kiro 协议链路涉及 AWS Builder ID 与风控，需控制频率与邮箱来源。
- 高并发受物理屏幕限制，超过槽位自动排队；更高并发需虚拟桌面方案。
- 凭证以 0600 文件 + SQLite 主库保存，暂不引入额外加密，避免影响现有导出工具链。
