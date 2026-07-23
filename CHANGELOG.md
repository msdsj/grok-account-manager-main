# 更新日志

本文根据最近 Git 提交和当前工作区改动整理。后续每次改版本时，先在前端更新公告里追加一句摘要，再把完整内容补到这里。

## 2026-07-23 - 控制台与后端结构重构

### 架构

- 后端源码统一迁移到 `serve/grok_account_manager/`，项目构建入口同步更新到 `pyproject.toml`。
- 后端重构为单一 FastAPI 服务，新增 `grok-account-manager-api` 启动命令，旧的 `grok-account-manager-web` 继续兼容。
- FastAPI 后端按 `api/routers/`、`api/services/`、`api/schemas.py`、`api/config.py` 拆分，注册任务、账号管理、中转站和代理接口分模块维护。
- 前端继续放在 `web/`，新增 `web/src/routes.tsx` 统一管理侧边栏路由和页面标题。
- 开发模式简化为两个进程：启动 FastAPI 后端，再启动 React/Vite 前端；前端代理 `/api`、`/v1` 和 `/admin/api` 到后端。

### 账号池与数据库

- 新增本地 SQLite 账号数据库，账号完整凭证、导出索引和测试结果会写入 `output/grok-account-manager.db`。
- 账号列表支持选择、批量测试、导出 JSON、导出 CPA、删除账号和刷新额度。
- 邮箱账号池兼容 `----` 和 `|` 两种分隔格式，Google/Gmail 账号池支持 `账号|密码|辅助邮箱` 格式。
- README、`.env.example` 和相关文档补充数据库、Google/Gmail 邮箱源和本地控制台运行说明。

### Grok 测试与中转

- 新增 Grok CLI 测试入口，用账号池账号验证 `grok-4.5` CLI 能力，并支持导出可用账号。
- 新增 Chat 对话测试入口，可选择账号池账号测试 `grok-4.5`、`grok-4.20` 和 `grok-4.3` 系列模型。
- 新增图片生成测试入口，默认使用 `grok-imagine-image-lite`，并支持标准、Pro 图片模型选项。
- 本地中转站统一到 FastAPI 后端，支持配置保存、启动/停止、同步账号、列出模型和 OpenAI 兼容代理接口。

### 前端体验

- 控制台重做为侧边栏路由布局，页面风格改成小清新毛玻璃和七彩泡泡背景。
- 系统总览聚焦账号资产、本地中转和任务日志，仪表盘不再展示注册任务卡片。
- 新增更新公告弹窗，顶部展示 GitHub 项目地址并引导用户帮忙点 Star。
- 首页新增 QQ 交流群板块，群号为 `972295238`，二维码资源放在 `web/public/community-qr.png`。
- README 替换为最新首页截图，展示图位于 `docs/images/dashboard.png`。

## 2026-07-22 - 账号检测与 Google 支持

- `7ac8e0c`：新增账号状态跟踪和超时处理机制，前端可展示账号可用性和检测结果。
- `394a60f`：新增 Google Workspace 速度限制页面处理，并补充 CPA 凭证导出能力。
- `0661356`：新增 Google/Gmail 邮箱支持，优化浏览器配置和 OAuth 流程，并加入本地中转能力。

## 2026-07-21 - 浏览器稳定性与验证处理

- `c98ffe9`：优化浏览器会话关闭逻辑，减少注册任务结束后的残留状态。
- `5d5e7fe`：支持自动点击 Cloudflare Turnstile 验证复选框，提升注册流程自动化稳定性。

## 2026-07-20 - 邮箱源与项目初始化

- `637fdf4`：新增微软邮箱登录和域名邮箱登录能力。
- `b23721e`：在 README 中加入项目界面截图。
- `c02f1a5`：新增 Outlook 邮箱支持，并开始整理项目结构。
- `6fa59f5` / `fd4e566`：项目初始化，支持域名邮箱注册基础流程。
