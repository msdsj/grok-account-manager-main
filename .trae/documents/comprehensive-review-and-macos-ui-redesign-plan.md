# 全面代码审查与 macOS 风格界面重构计划

## Context

当前项目已形成 Grok、GPT、Kiro 三网关统一平台，包含账号注册、导入导出、额度刷新、账号测试、日志、模型目录、网关路由和客户端密钥等能力。现有未提交改动较多，核心业务逻辑已基本完成，但管理接口权限边界、默认密钥暴露、CORS、重复路由注册、输入边界和前后端契约仍需系统复核。同时 React 控制台虽然具备 macOS 色彩和窗口基础，但页面仍存在卡片同质化、导航密集、表格拥挤、注册表单过长和页面层级不清的问题。

本次目标是在不破坏现有 provider、账号池、网关适配器和注册任务核心逻辑的前提下，先完成高置信代码问题修复，再建立统一的 macOS 视觉基础和页面布局，最后进行完整构建、测试和浏览器回归验证。所有修改保持小批次、可追溯，不覆盖或回退当前工作区已有改动。

## Recommended Approach

### 1. 建立变更基线与审查清单

- 记录当前工作区状态和前后端变更范围，区分已有功能与本次修复内容。
- 梳理公开接口、管理员接口、客户端网关接口三类认证边界。
- 对 Grok/GPT/Kiro 注册、账号导入导出、单项测试、批量测试、日志、密钥、仪表盘逐项核对前后端契约。
- 重点复用现有服务层和 provider/gateway 实现，不重写注册与网关核心流程。

### 2. 修复高置信后端与接口问题

关键文件：

- `serve/grok_account_manager/api/app.py`
- `serve/grok_account_manager/api/routers/admin_auth.py`
- `serve/grok_account_manager/api/routers/accounts.py`
- `serve/grok_account_manager/api/routers/gateway.py`
- `serve/grok_account_manager/api/routers/logs.py`
- `serve/grok_account_manager/api/routers/admin_stats.py`
- `serve/grok_account_manager/api/routers/register.py`
- `serve/grok_account_manager/api/routers/mailboxes.py`
- `serve/grok_account_manager/gateway/auth.py`

处理内容：

- 删除 `app.py` 中重复的 `state.router` 注册。
- 将管理员认证整理为可复用的依赖或统一校验入口，覆盖账号、注册任务、邮箱配置、日志、统计和网关管理接口；保留登录、刷新、健康检查及对外 `/v1` 客户端 key 认证语义。
- 保护 `/api/gateway/keys/default`，未授权请求不得读取默认密钥明文；前端默认只显示掩码，明文只在管理员主动操作时短暂返回。
- 收紧 CORS，默认允许本地 43188 开发源，生产环境通过环境变量显式配置来源。
- 统一空选择、无效 provider/model、资源不存在、批量数量、导入数据大小、测试参数和分页参数的校验及错误结构。
- 保留账号导出 `no-store`，检查审计日志和异常文本不写入 token、cookie、refresh token 等敏感内容。
- 补充接口权限、默认 key 幂等、CORS 和边界输入测试。

### 3. 建立统一前端 macOS 设计基础

关键文件：

- `web/src/index.css`
- `web/src/shared/components/page-header.tsx`
- `web/src/shared/components/data-table-shell.tsx`
- `web/src/features/dashboard/dashboard-panel.tsx`
- 必要时新增或增强现有基础 UI 组件，但不改变业务 API。

处理内容：

- 统一窗口背景、侧栏、工具栏、卡片、浮层、分割线、文本层级、状态色、圆角和阴影 tokens。
- 增强浅色/深色主题对比度、键盘 focus、缩小动效、表格横向滚动和窄屏可用性。
- 提炼 `PageFrame`/页面容器、标题区、macOS 卡片、指标卡、工具栏、空态/错误态和表格壳等复用结构；优先改造现有组件，避免新增无必要抽象。
- 减少业务页面中重复的长 Tailwind class，让页面只表达结构与状态。

### 4. 大幅改造应用壳层与导航

文件：

- `web/src/app/app-shell.tsx`
- `web/src/index.css`

设计方向：

- 保留现有路由、鉴权、懒加载、侧栏收起状态、移动端 Sheet、主题和语言切换。
- 采用更明确的 macOS 窗口结构：半透明侧栏、轻量顶部 toolbar、居中主内容区和稳定的页面留白。
- 降低导航密度，按概览、账号资源、生产任务、网关、系统进行清晰分组；低频文档和设置保留折叠或次级入口。
- 统一当前页高亮、窗口标题栏、管理员入口、主题切换和移动端导航行为。

### 5. 改造核心业务页面

#### Dashboard

文件：`web/src/features/dashboard/*`

- 第一屏突出三类账号数量、可用凭证、额度、运行状态和异常状态。
- 减少同质卡片堆叠，改为主指标、账号池状态、趋势和最近活动的层级结构。
- 保留现有查询、周期选择、刷新和 provider 数据；分别处理加载、空数据和局部失败。

#### 账号池

文件：

- `web/src/features/managed-accounts/managed-accounts-page.tsx`
- `web/src/features/managed-accounts/account-pool-page.tsx`
- `web/src/shared/components/data-table-shell.tsx`

- 顶部形成稳定工具栏，统一搜索、标签、筛选、刷新和批量操作。
- 表格明确邮箱、状态、额度、能力、标签、测试和操作优先级；敏感字段默认掩码。
- 保留 Grok/Kiro/GPT 专属页面、导入导出、删除、标签、额度刷新和三类测试入口。
- 窄屏使用横向滚动或详情抽屉，不压缩到不可读。

#### 注册任务

文件：

- `web/src/features/register-task/register-task-page.tsx`
- `web/src/features/register-task/register-task-api.ts`

- 将长表单整理为平台与数量、邮箱来源、代理并发、平台专属设置、确认启动几个视觉分区或 disclosure。
- 继续按 Grok/GPT/Kiro 动态显示字段，代理池默认关闭，自动换取 refresh_token，仅 Grok 显示 Sub2API。
- 保留启动、停止、重试、实时状态、邮箱配置和代理池保存逻辑；增加清晰任务摘要和状态层级。

#### 日志、测试、密钥

文件：

- `web/src/features/logs/operation-logs-page.tsx`
- `web/src/features/account-tests/account-test-board-page.tsx`
- `web/src/features/client-keys/client-keys-page.tsx`

- 日志采用统一 tabs/segmented control、工具栏筛选、摘要表格和详情弹层。
- 测试页面分离账号选择、模式参数和结果区，按 provider 显示能力，保留额度消耗提示和运行状态。
- 密钥页面区分默认系统密钥和普通客户端密钥；默认掩码，创建后明文仅一次性展示，复制/删除使用一致的确认和反馈。

### 6. 规范化与清理

- 清理未使用 import、重复 class、不可达分支和重复 API helper。
- 统一 Python snake_case、React PascalCase/camelCase、中文 UI 文案和 provider 命名。
- 只删除确认无调用的旧代码，不触碰 `output/` 中用户生成凭据，不提交 `.env`、cookie、token 或导出文件。
- 保持核心逻辑不变，所有行为变更通过测试和接口契约确认。

## Verification

后端：

```bash
uv run python -m unittest discover -s tests
uv run python -m compileall -q serve tests
```

前端：

```bash
cd web && npm run build
```

全链路：

```bash
./scripts/verify.sh
```

回归重点：

- 未登录访问管理接口返回 401，管理员登录后账号、注册、日志、统计、密钥接口正常。
- 默认密钥初始化幂等，未授权不能读取明文，网关带 key 的 `/v1/models` 正常。
- Grok/Kiro/GPT 三类账号池切换、导入导出、标签、额度刷新、聊天/生图/视频能力限制正常。
- 注册 provider 和邮箱来源切换时只显示对应配置，任务可启动、停止、重试并正确展示状态。
- 日志 tabs、筛选、详情、测试模式和密钥复制/删除流程正常。
- 桌面/窄屏、浅色/深色、加载/空态/错误态、键盘 focus 和 reduced-motion 正常。
- 浏览器控制台无未处理异常，敏感 token 不出现在 URL、日志或前端持久化存储中。
