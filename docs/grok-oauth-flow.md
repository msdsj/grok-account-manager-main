# Grok OAuth 流程说明

这份文档只做研究索引，不改实现。

## 当前流程

1. `src/ai_signuper/__main__.py` 读取 `--oauth-exchange` 等参数。
2. `src/ai_signuper/providers/grok.py` 完成注册，拿到 `sso cookie`，再决定是否进入 OAuth 交换。
3. `src/ai_signuper/grok_oauth_exchange.py` 负责 discovery、PKCE、启动本地回调、打开授权页、先点“使用邮箱登录”、再推进到 callback、最后换 token。
4. `src/ai_signuper/grok_api.py` 把 token 和用户信息整理成 cockpit-tools 可导入的 JSON 结构。
5. `src/ai_signuper/sinks/json_credential.py` 负责最终 JSON 落盘。
6. 参考实现可以看 `/Users/meishiduoshuijiao/Desktop/cockpit-tools-main/sidecars/cockpit-cliproxy/cdk/CLIProxyAPI/internal/auth/xai/xai.go` 和 `internal/auth/xai/token.go`。

## 你要看哪些文件

- `src/ai_signuper/__main__.py`
- `src/ai_signuper/providers/grok.py`
- `src/ai_signuper/grok_oauth_exchange.py`
- `src/ai_signuper/grok_api.py`
- `src/ai_signuper/sinks/json_credential.py`

## 如果你想研究“邮箱登录/下一步”这一步

主要看 `src/ai_signuper/providers/grok.py` 里注册入口，以及 `src/ai_signuper/grok_oauth_exchange.py` 里页面驱动逻辑。

这里的职责是：

- 先点“使用邮箱登录”
- 填邮箱
- 识别当前页面状态
- 继续推进到授权页
- 等待本地 callback

如果你要自己改流程，先确认这一层是不是还停在邮箱页，还是已经进入了 OAuth consent 页。

## 如果你想研究“为什么一直在等 callback”

重点看 `src/ai_signuper/grok_oauth_exchange.py` 里的这几块：

- discovery 结果有没有拿到
- 本地 `127.0.0.1:56121/callback` 有没有成功启动
- 授权页有没有真的跳回 callback
- `callback_queue` 有没有收到 `code` 和 `state`

如果页面没跳回本地 callback，后面的 token 交换永远不会开始。

## 如果你想研究“为什么 `refresh_token` 还是空”

优先看这两个地方：

- `src/ai_signuper/providers/grok.py` 里 OAuth 失败后有没有回退到 `sso cookie`
- `src/ai_signuper/grok_api.py` 里 `oauth_tokens` 有没有真的进入最终 credential

常见情况是：OAuth 没成功，但代码还是生成了一个看起来正常的 JSON，于是 `refresh_token` 只能是 `null`。

## 如果你想研究 token endpoint

看 `src/ai_signuper/grok_oauth_exchange.py`：

- `._discover_oauth_endpoints()`
- `._exchange_code_for_tokens()`

原则是：

- discovery 给什么，就优先用什么
- 不要在后面又硬编码覆盖掉

## 建议的修改顺序

1. 先把授权页真正走到 callback。
2. 再确认 token exchange 返回里有 `refresh_token`。
3. 再确认 `grok_api.py` 把完整 token 写进最终 credential。
4. 最后再看 `json_credential.py` 的落盘结果。

## 参考项目对照

参考项目里 xAI 相关入口主要是：

- `internal/auth/xai/xai.go`
- `internal/auth/xai/token.go`
- `sdk/auth/xai.go`
- `internal/api/handlers/management/auth_files.go`

它的思路也是标准 PKCE loopback，不是先拿一个半成品 JSON 再补字段。
