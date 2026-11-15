# ChatGPT 注册流程说明

## 概述

ChatGPT 自动注册实现基于浏览器自动化（DrissionPage），通过 JavaScript 注入实现页面交互。

## 技术来源

本实现整合了以下项目的最佳实践：

1. **any-auto-register**: HTTP-based 注册引擎架构
2. **chatgpt2api**: Sentinel/PoW/Turnstile 工具函数
3. **grok-account-manager**: DrissionPage 浏览器自动化模式

## 注册流程

### 1. 准备阶段
- 创建临时邮箱（DuckMail/Outlook/Gmail）
- 初始化浏览器会话
- 设置用户代理和语言

### 2. 打开注册页
- 访问 `https://chatgpt.com/`
- 等待页面加载完成
- 查找并点击 "Sign up" 按钮

### 3. 填写邮箱
- 在邮箱输入框填写临时邮箱地址
- 触发 input/change 事件
- 点击 "Continue" 按钮

### 4. 邮箱验证
- OpenAI 发送 6 位数字验证码到邮箱
- 通过邮箱服务 API 读取验证码
- 在验证码输入框填写
- 页面自动验证并跳转

### 5. 设置密码
- 生成符合要求的随机密码：
  - 至少 8 个字符
  - 包含小写字母、大写字母、数字、特殊字符
- 在密码输入框填写
- 点击 "Continue" 或 "Sign up" 按钮

### 6. 获取凭证
- 等待 `__Secure-next-auth.session-token` cookie 出现
- 提取 session token
- 返回注册结果

## 关键技术点

### 浏览器自动化
使用 `page.run_js()` 注入 JavaScript：
- 避免被反爬检测
- 模拟真实用户操作
- 触发正确的 React 事件

### 邮箱验证码读取
支持多种邮箱服务：
- **DuckMail**: 公共临时邮箱 API
- **Outlook IMAP/Graph**: 微软账号（推荐）
- **Gmail IMAP**: Google 账号

### 密码生成策略
```python
# 确保包含所有必需字符类型
password = [lowercase, uppercase, digit, special] + random_chars
random.shuffle(password)
```

### 错误处理
- 页面刷新检测
- 元素丢失恢复
- 超时重试机制
- 邮箱池耗尽检测

## Sentinel Token 支持

虽然当前浏览器自动化流程不需要手动处理 Sentinel，但 `chatgpt_utils.py` 提供了完整的实现：

### SentinelTokenGenerator
- FNV-1a 32位哈希算法
- PoW (Proof of Work) 求解
- 支持 requirements token 和 proof token

### 使用场景
- HTTP-based 注册（未来扩展）
- 密码登录验证
- OAuth 流程增强

## 配置选项

### 环境变量
```bash
# 最大注册重试次数（默认 3）
CHATGPT_ACCOUNT_MANAGER_MAX_REGISTRATION_ATTEMPTS=3

# OpenAI 服务地址（可选，用于镜像）
OPENAI_AUTH_BASE_URL=https://auth.openai.com
CHATGPT_APP_URL=https://chatgpt.com
```

### Provider 属性
```python
provider = ChatGPTProvider()
provider.mail_source = DuckMailSource()  # 邮箱源
provider.event_callback = log_callback   # 日志回调
provider.stop_event = threading.Event()  # 停止信号
provider.max_registration_attempts = 3   # 重试次数
```

## 常见问题

### Q: 验证码获取超时
**A**: 检查邮箱服务是否正常，增加超时时间（默认 180 秒）

### Q: 密码被拒绝
**A**: OpenAI 对纯字母数字密码拒绝率较高，确保包含特殊字符

### Q: Session token 未获取到
**A**: 等待时间可能不足，或页面跳转失败，检查浏览器日志

### Q: 页面元素找不到
**A**: OpenAI 可能更新了前端代码，需要更新选择器逻辑

## 未来扩展

### 1. HTTP-based 注册引擎
- 无需浏览器，速度更快
- 需要处理 Sentinel/Turnstile
- 需要维护 cookies 和 session

### 2. OAuth Refresh Token
- 通过 Codex CLI OAuth 流程
- 获取长期有效的 refresh_token
- 支持 API 调用

### 3. 账号质量检测
- 检测账号是否有 Plus 权限
- 检测模型可用性（GPT-4, DALL-E 3）
- 自动标记账号状态

## 代码结构

```
serve/grok_account_manager/providers/
├── chatgpt.py          # 主注册流程（浏览器自动化）
├── chatgpt_utils.py    # Sentinel/PoW 工具函数
└── base.py             # Provider 协议定义
```

## 维护建议

1. **定期更新选择器**: OpenAI 前端更新时需要调整元素查找逻辑
2. **监控成功率**: 跟踪注册成功率，及时发现问题
3. **邮箱池管理**: 确保邮箱服务稳定可用
4. **代理配置**: 某些地区可能需要代理访问

## 参考资源

- [OpenAI Auth Flow](https://auth.openai.com/)
- [DrissionPage 文档](https://drissionpage.cn/)
- [any-auto-register](https://github.com/...)
- [chatgpt2api](https://github.com/basketikun/chatgpt2api)
