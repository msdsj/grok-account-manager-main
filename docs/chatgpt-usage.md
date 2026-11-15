# ChatGPT 注册使用示例

## 基本使用

### 1. 命令行使用

```bash
# 使用 DuckMail 注册 1 个 ChatGPT 账号
uv run grok-account-manager chatgpt --count 1 --sink json+txt

# 使用 Outlook 邮箱注册（需要配置）
uv run grok-account-manager chatgpt --count 5 --mail-source outlook --sink json

# 使用自定义邮箱池注册
uv run grok-account-manager chatgpt --count 10 --mail-pool ./emails.txt --sink sub2api
```

### 2. Python API 使用

```python
from grok_account_manager.core.browser import DrissionBrowserSession
from grok_account_manager.providers.gpt.registration import ChatGPTProvider
from grok_account_manager.mail.sources import DuckMailSource

# 创建 provider
provider = ChatGPTProvider()
provider.mail_source = DuckMailSource()

# 设置日志回调
def log_callback(level, message, **kwargs):
    print(f"[{level.upper()}] {message}")

provider.event_callback = log_callback

# 运行注册
with DrissionBrowserSession() as session:
    result = provider.run_round(session)
    print(f"注册结果: {result}")
    print(f"邮箱: {result['email']}")
    print(f"Session Token: {result['credential'][:20]}...")
```

### 3. Web 控制面板使用

1. 启动 API 服务器：
   ```bash
   uv run grok-account-manager-api
   ```

2. 启动 Web 前端：
   ```bash
   cd web && npm run dev
   ```

3. 在浏览器打开 `http://localhost:43188`

4. 在"注册任务"页面选择 ChatGPT provider

5. 配置参数：
   - 注册数量
   - 邮箱源（DuckMail/Outlook/Gmail）
   - 输出格式（JSON/TXT/Sub2API）

6. 点击"开始注册"

## 配置选项

### 邮箱源配置

#### DuckMail（默认，无需配置）
```python
from grok_account_manager.mail.sources import DuckMailSource
provider.mail_source = DuckMailSource()
```

#### Outlook IMAP
```python
from grok_account_manager.mail.sources import OutlookIMAPSource

# 在 output/mailboxes/outlook-accounts.txt 中配置：
# email@outlook.com----password----client_id----refresh_token----auto

provider.mail_source = OutlookIMAPSource()
```

#### Gmail IMAP
```python
from grok_account_manager.mail.sources import GmailIMAPSource

# 在 output/mailboxes/gmail-accounts.txt 中配置：
# email@gmail.com----password----recovery@email.com

provider.mail_source = GmailIMAPSource()
```

### 环境变量

```bash
# .env 文件配置

# ChatGPT 相关
CHATGPT_ACCOUNT_MANAGER_MAX_REGISTRATION_ATTEMPTS=3

# OpenAI 服务地址（可选，用于镜像）
OPENAI_AUTH_BASE_URL=https://auth.openai.com
CHATGPT_APP_URL=https://chatgpt.com
SENTINEL_BASE_URL=https://sentinel.openai.com

# 邮箱服务配置
DUCKMAIL_API_URL=https://www.1secmail.com/api/v1/

# 代理配置（可选）
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
```

## 输出格式

### JSON 输出
```json
{
  "email": "test@example.com",
  "credential": "session-token-here",
  "profile": {
    "email": "test@example.com",
    "password": "GeneratedPassword123!",
    "auth_provider": "email"
  },
  "oauth_status": "not_requested"
}
```

### TXT 输出
```
test@example.com----GeneratedPassword123!----session-token-here
```

### Sub2API 输出
直接推送到 Sub2API 服务器：
```bash
# 配置 .env
SUB2API_BASE_URL=https://your-sub2api-server.com
SUB2API_ADMIN_API_KEY=your-admin-key

# 运行
uv run grok-account-manager chatgpt --count 5 --sink sub2api
```

## 高级特性

### 1. 批量注册

```python
import asyncio
from concurrent.futures import ThreadPoolExecutor

def register_one():
    with DrissionBrowserSession() as session:
        provider = ChatGPTProvider()
        provider.mail_source = DuckMailSource()
        return provider.run_round(session)

# 并行注册 10 个账号
with ThreadPoolExecutor(max_workers=3) as executor:
    futures = [executor.submit(register_one) for _ in range(10)]
    results = [f.result() for f in futures]
```

### 2. 停止信号

```python
import threading

stop_event = threading.Event()
provider.stop_event = stop_event

# 在另一个线程中
def stop_after_delay():
    time.sleep(30)  # 30秒后停止
    stop_event.set()

threading.Thread(target=stop_after_delay, daemon=True).start()
```

### 3. 自定义日志

```python
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def log_callback(level, message, **kwargs):
    stage = kwargs.get('stage', 'unknown')
    if level == 'error':
        logger.error(f"[{stage}] {message}")
    elif level == 'warning':
        logger.warning(f"[{stage}] {message}")
    elif level == 'success':
        logger.info(f"✓ [{stage}] {message}")
    else:
        logger.info(f"[{stage}] {message}")

provider.event_callback = log_callback
```

### 4. 浏览器窗口标签

```python
provider.browser_window_label = "ChatGPT 注册 #1"
# 在浏览器窗口右上角显示标签
```

## 故障排查

### 常见错误

#### 1. "邮箱池已耗尽"
**原因**: DuckMail 获取邮箱失败
**解决**: 
- 检查网络连接
- 尝试更换邮箱源（Outlook/Gmail）
- 检查 DuckMail API 是否可用

#### 2. "获取验证码失败"
**原因**: 邮箱服务未收到验证码
**解决**:
- 增加超时时间（默认 180 秒）
- 检查邮箱服务是否正常
- 查看 OpenAI 是否标记邮箱为垃圾

#### 3. "密码被拒绝"
**原因**: 密码不符合 OpenAI 要求
**解决**:
- 检查密码生成逻辑
- 确保包含小写、大写、数字、特殊字符
- 不要使用过于简单的密码

#### 4. "页面元素找不到"
**原因**: OpenAI 更新了前端代码
**解决**:
- 更新选择器逻辑
- 查看浏览器控制台错误
- 提交 issue 或 PR

### 调试模式

```python
# 启用详细日志
import logging
logging.basicConfig(level=logging.DEBUG)

# 保持浏览器窗口打开
session = DrissionBrowserSession()
# 手动观察注册流程
result = provider.run_round(session)
# session 会在 with 块结束后关闭
```

## 性能优化

### 1. 邮箱池预热
```bash
# 提前创建 Outlook/Gmail 账号
# 配置到 output/mailboxes/ 目录
# 避免注册时等待邮箱创建
```

### 2. 代理配置
```bash
# 使用高质量住宅代理
# 避免数据中心 IP 被标记
HTTP_PROXY=http://residential-proxy:port
```

### 3. 并发控制
```python
# 不要设置过高的并发数
# 推荐 2-5 个浏览器实例
max_workers = 3
```

## 最佳实践

1. **使用 Outlook/Gmail 邮箱**: 比 DuckMail 更稳定
2. **启用代理**: 避免 IP 被限制
3. **分批注册**: 每批 10-20 个，避免过度频繁
4. **保存密码**: 确保密码和邮箱对应关系正确保存
5. **定期轮换**: 不要长期使用同一批账号
6. **监控成功率**: 低于 80% 需要检查问题
7. **备份输出**: 定期备份 output/ 目录

## 集成示例

### 与 Sub2API 集成
```python
from grok_account_manager.sinks.sub2api import Sub2APISink

# 配置 Sub2API
sink = Sub2APISink(
    base_url="https://your-sub2api-server.com",
    admin_api_key="your-admin-key"
)

# 注册并推送
result = provider.run_round(session)
sink.save(result)
```

### 与数据库集成
```python
import sqlite3

def save_to_db(result):
    conn = sqlite3.connect('accounts.db')
    c = conn.cursor()
    c.execute('''
        INSERT INTO chatgpt_accounts (email, password, session_token, created_at)
        VALUES (?, ?, ?, datetime('now'))
    ''', (result['email'], result['profile']['password'], result['credential']))
    conn.commit()
    conn.close()

result = provider.run_round(session)
save_to_db(result)
```

## 更新日志

### v1.0.0 (2025-01-XX)
- ✅ 完整的浏览器自动化注册流程
- ✅ 支持 DuckMail/Outlook/Gmail 邮箱源
- ✅ Sentinel Token 生成工具
- ✅ 增强的错误处理和日志
- ✅ Web 控制面板集成
- ✅ 多种输出格式支持

### 未来计划
- [ ] HTTP-based 注册引擎（无浏览器）
- [ ] OAuth Refresh Token 获取
- [ ] 账号质量检测（Plus/GPT-4）
- [ ] 自动凭证轮换
- [ ] 批量账号管理

## 贡献

欢迎提交 PR 改进 ChatGPT 注册流程！

请确保：
1. 代码通过 `uv run python -m compileall` 检查
2. 测试至少成功注册 5 个账号
3. 更新相关文档
4. 遵循现有代码风格
