# Codex 智谱 Responses/SSE 诊断代理

这是一个仅监听 `127.0.0.1` 的透明中间层：

```text
Codex ──HTTP/SSE──> 127.0.0.1:8765 ──HTTP/SSE──> https://open.bigmodel.cn
```

它不把 Responses API 转换为 Chat Completions，也不管理 Codex session。主要用途是：

- 强制智谱上游使用 HTTP/1.1；
- 默认每个请求新建上游连接，避免复用被中间设备污染的连接；
- 上游静默时，向本机 Codex 发送 SSE 注释心跳；
- 用中文记录 DNS、TLS、首包、SSE 事件、静默时间和断流位置；
- 提前断流时可发出标准 `response.failed`，让 Codex 显示本地诊断请求号和断流原因；
- 一键生成脱敏诊断包，便于后续分析。

这里的“流”是 Responses API 的 **HTTP SSE 长响应**，通常不是 WebSocket。现象确实很像长响应被中间设备、上游网关或空闲超时切断，但仅凭“重连 5 次”还不能锁定是奇安信。

## 会不会丢失 session 上下文？

正常情况下不会。代理是无状态透明转发：

- `input`、`previous_response_id`、工具调用结果和相关请求头均原样发送给智谱；
- 代理重启不会删除 Codex 已保存的会话；
- 只有重启时正在生成的那一轮会中断，之前已完成的上下文不受影响；
- 发生半途断流时，当前轮会明确失败，需要重发，但不会清空此前会话。

需要注意：若上游已执行了请求却在返回途中断开，Codex 无法确认当前轮是否完成；重发可能产生另一份回答，涉及工具调用时也可能造成重复副作用。因此代理不会自动重发上游请求，诊断阶段也建议把 `stream_max_retries` 设为 `0`。

## 为什么界面恰好重连 5 次？

Codex 自定义模型提供商的 `stream_max_retries` 默认值就是 5。它说明 Codex 没有看到完整的 SSE 终止事件，不能单独证明是奇安信、智谱还是其他网关导致。

本代理会把故障进一步分成：

1. 尚未收到 HTTP 响应头就失败；
2. 已收到响应头，但首个字节前失败；
3. 已收到部分 SSE，随后异常/EOF；
4. Codex/本机客户端先关闭连接；
5. 智谱明确返回鉴权、限额、WAF 或服务端错误。

## Windows 快速使用

### 1. 安装依赖

在项目目录打开 PowerShell：

```powershell
python -m pip install -r requirements.txt
```

如果之前已运行过 `codex-nvidia-proxy`，通常已有 Flask、Waitress 和 OpenAI SDK 自带的 httpx，但仍建议执行一次上述命令。

### 2. 可选配置

```powershell
Copy-Item .env.example .env
notepad .env
```

初次诊断建议保留默认值：

```text
BIGMODEL_FORCE_HTTP1=1
BIGMODEL_CONNECTION_CLOSE=1
BIGMODEL_LOCAL_HEARTBEAT=10
BIGMODEL_LOG_BODY=0
```

### 3. 启动

双击 `run.cmd`，或在 PowerShell 执行：

```powershell
.\start.ps1
```

两个 PowerShell 脚本均使用带 BOM 的 UTF-8 保存，以兼容 Windows PowerShell 5.1 对中文脚本的读取规则。

看到以下地址即为成功：

```text
控制台:       http://127.0.0.1:8765/
Codex Base URL: http://127.0.0.1:8765/api/v1
```

不要关闭代理窗口。

### 4. 修改 Codex 配置

参考 `codex-provider.example.toml`，把原来的智谱 Provider 改为：

```toml
[model_providers.ZAI_LOCAL_DIAG]
name = "智谱（本地诊断代理）"
base_url = "http://127.0.0.1:8765/api/v1"
experimental_bearer_token = "<Your API Key>"
wire_api = "responses"
request_max_retries = 1
stream_max_retries = 0
stream_idle_timeout_ms = 600000
```

`model_provider` 同时改为：

```toml
model_provider = "ZAI_LOCAL_DIAG"
```

关闭所有 Codex 窗口后重新启动。原有会话不会被删除。

这里把 `stream_max_retries` 设为 `0` 是为了让一次故障只产生一条上游请求并保留最清晰的证据。实测即使代理合成了 `response.failed`，Codex 仍会按该参数重试；中间层无权覆盖客户端的重试策略。链路稳定后可以再改为 `1` 或更高。

## 日志位置

```text
logs/proxy.log       人类可读的详细中文日志
logs/events.jsonl    便于精确分析的结构化日志
```

控制台页面 `http://127.0.0.1:8765/` 可查看最近日志并下载诊断包。

日志默认不记录：

- `Authorization`；
- Cookie；
- API Key；
- 对话正文。

如确实要分析请求结构，可临时设置：

```text
BIGMODEL_LOG_BODY=1
```

它仍会隐藏凭据并截断长正文。问题复现后应立即改回 `0`。

## 复现后如何打包

浏览器点击“下载诊断包”，或执行：

```powershell
python .\bigmodel_diagnostic_proxy.py pack
.\collect-diagnostics.ps1
```

将 `logs` 中最新的两个 ZIP 提供给分析人员即可。打包脚本只提取 Codex Provider 的非敏感配置，不复制 `experimental_bearer_token`。

## 日志判读

### `上游连接失败`

未收到智谱 HTTP 响应头。优先检查 DNS、TLS 证书、显式代理和安全软件日志。

### `上游静默`

已建立 SSE，但一段时间没有新字节。代理会向 Codex 发本地注释心跳；这只能保持 `Codex → 本地代理`，不能修复 `本地代理 → 智谱` 的断链。

### `上游静默且事件未闭合`

上游停在半个 SSE 事件/JSON 中。此时代理不会插入心跳，以免破坏事件内容；日志会记录尚未闭合的字节数。

### `上游提前断流`

已读到 EOF，却没有 `response.completed` / `response.failed` / `[DONE]`。这是判断“长连接被中途切断”的关键证据。

### `Codex侧断开`

上游仍在读取时本机客户端先关闭，需结合 Codex 日志和安全软件事件判断。

### `上游读取异常`

已经收到部分数据，但 TCP/TLS/HTTP 消息体在读取时异常。日志中的异常类型、已收字节数、最后事件和静默时长是定位中间链路的关键字段。

### `上游HTTP错误`

智谱明确返回状态码。日志会保留脱敏后的错误摘要与 `X-LOG-ID`、`ga-traceid` 等追踪头，完整错误响应仍原样返回给 Codex。

## 重要限制

- 代理不会在收到部分输出后自动重发请求，避免重复生成或重复工具调用。
- 本地 SSE 心跳无法阻止企业网关切断上游 TCP；它的价值是隔离两段链路并留下证据。
- 如果目标机证明确实只拦截长 SSE，后续可在此项目上增加“上游非流式、下游重放 SSE”的兼容模式；第一版不盲目改写协议。
- 只允许绑定回环地址（`127.0.0.1` / `localhost` / `::1`），代码会拒绝局域网或公网监听。

## 后续反馈时提供什么

复现后尽量提供以下四项，不要单独复制 API Key：

1. 故障发生的大致时间；
2. Codex 界面最后一条错误文字；
3. `proxy.log` 中对应的 `bm_时间_随机串` 请求号；
4. `collect-diagnostics.ps1` 生成的两个最新 ZIP。

这样可以判断断点是在响应头之前、首字节之前、SSE 中途、智谱明确报错，还是 Codex 客户端先断开。

## 命令

```powershell
# 启动代理
python .\bigmodel_diagnostic_proxy.py serve

# 不用 API Key，单独检查 DNS/TLS 并打包
python .\bigmodel_diagnostic_proxy.py doctor

# 打包已有代理日志
python .\bigmodel_diagnostic_proxy.py pack

# 完全离线的本地自检，不访问智谱，也不使用 API Key
python .\self_test.py
```

当前自检覆盖：正常完成、事件间静默心跳、半事件静默、提前 EOF、读取异常、401/429/503、普通响应、压缩 SSE、客户端主动断开、日志脱敏和 Waitress 参数兼容性。
