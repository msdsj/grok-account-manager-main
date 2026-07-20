# DuckMail 接入说明

项目已从 Mail.tm 迁移到 DuckMail 自有域名邮箱方案。

## 配置步骤

### 1. 复制配置模板

```bash
cp .env.example .env
```

### 2. 配置 DuckMail 环境变量

在 `.env` 文件中填入你的 DuckMail 配置：

```bash
DUCKMAIL_BASE_URL=https://api.duckmail.sbs
DUCKMAIL_API_KEY=your_duckmail_api_key
DUCKMAIL_DOMAIN=@msdsj.cyou
```

### 3. 测试运行

```bash
# 确保依赖已安装
uv sync

# 单轮测试
uv run python -m ai_signuper grok --count 1

# 如果需要推送到 Sub2API
uv run python -m ai_signuper grok --count 1 --sink sub2api
```

## DuckMail vs Mail.tm

| 特性 | Mail.tm (旧) | DuckMail (新) |
|------|-------------|--------------|
| 域名 | 公共随机域名 | 自有域名 @msdsj.cyou / @msdsj.asia |
| API 认证 | 无需 API Key | 需要 API Key 创建邮箱 |
| 稳定性 | Python 3.14 有 TLS 问题 | 待验证 |
| 邮箱地址 | 完全随机 | 可控域名 + 随机前缀 |

## 技术细节

### API 端点变化

**Mail.tm**:
```python
BASE_URL = "https://api.mail.tm"
# 需要先获取可用域名列表
GET /domains
POST /accounts  # 无需认证
POST /token     # 无需认证
GET /messages   # Bearer {邮箱token}
```

**DuckMail**:
```python
BASE_URL = "https://api.duckmail.sbs"
# 域名固定，无需查询
POST /accounts  # Bearer {API_KEY}
POST /token     # 无需认证
GET /messages   # Bearer {邮箱token}
```

### 环境变量

- `DUCKMAIL_BASE_URL` **(可选)**: DuckMail API 地址，默认 `https://api.duckmail.sbs`
- `DUCKMAIL_API_KEY` **(必需)**: DuckMail 服务的 API Key
- `DUCKMAIL_DOMAIN` **(可选)**: 邮箱域名，默认 `@msdsj.cyou`，也支持 `@msdsj.asia`

### 代码改动

核心改动位于 `src/ai_signuper/mail_otp.py`:

1. **替换 API 端点**: `api.mail.tm` → `api.duckmail.sbs`
2. **添加 API Key 认证**: 创建邮箱时需要 `Authorization: Bearer {DUCKMAIL_API_KEY}`
3. **固定域名**: 不再动态获取域名，使用配置的 `DUCKMAIL_DOMAIN`
4. **处理 409**: 邮箱已存在时继续流程而非报错

### 验证码提取

验证码提取逻辑 (`_extract_code`) **保持不变**，继续支持：

- xAI 格式: `WVB-8OE` (主题或正文)
- 6位数字: `123456`
- 中文验证码: `验证码为 123456`
- OpenAI 格式: `code: 123456`

## 故障排查

### 1. 邮箱创建失败

```
[Error] 创建邮箱失败: 401 - Unauthorized
```

**原因**: `DUCKMAIL_API_KEY` 未配置或无效

**解决**: 检查 `.env` 文件中的 `DUCKMAIL_API_KEY`

### 2. 域名不支持

```
[Error] 创建邮箱失败: 400 - Invalid domain
```

**原因**: 配置的域名不被 DuckMail 支持

**解决**: 使用 `@msdsj.cyou` 或 `@msdsj.asia`

### 3. Token 获取失败

```
[Error] 无法获取 DuckMail token
```

**原因**: 邮箱密码不匹配或邮箱不存在

**解决**: 检查代码逻辑，确保创建邮箱成功后再获取 token

## 测试验证

手动测试 DuckMail 接码流程（使用参考项目脚本）：

```bash
cd /Users/meishiduoshuijiao/Desktop/codex_auto_register-main
python3 check_mail.py test@msdsj.cyou
# 输入密码后会开始监听邮件
```

## 注意事项

1. **API Key 保密**: 不要把 `.env` 文件提交到 Git（已在 `.gitignore` 中）
2. **域名限制**: 目前只支持 `@msdsj.cyou` 和 `@msdsj.asia`
3. **密码长度**: DuckMail 要求密码至少 6 位（当前生成 16 位随机密码）
4. **Python 版本**: 保持 3.12/3.13，虽然 DuckMail 可能不受 3.14 TLS 问题影响，但未实测

## 参考资料

- 参考项目: `/Users/meishiduoshuijiao/Desktop/codex_auto_register-main`
- DuckMail API 文档: `codex_auto_register-main/duckmaildoc.md`
- 原接码脚本: `codex_auto_register-main/check_mail.py`
