# 修复方案：CLI 付费测试暴露的 3 个生产问题

> 基线：`origin/main@7c041820` ｜ 修复分支：`fix/cli-paid-bugs-20260826`
> worktree：`~/Desktop/huangque-fix-cli-paid-bugs-20260826`
> 全部基于服务器真实日志定位，不猜。

## 根因总结（服务器日志实证）

| # | 现象 | 根因 | 代码位置 |
|---|---|---|---|
| 1 | image-generate(banana) HTTP 400 | Gemini 上游返回 400，egress 把 HTTPError(非429) 当"已送达"直接抛出退点——**但 400 常是请求本身的问题(模型名/参数/key)，应该读响应体给出具体错误，而不是只报"HTTP Error 400"** | `egress.py:196-207` |
| 2 | collect-transcript ASR 超时 75s | `video_compose_asr.py:161` timeout=`max(120, duration*5)`，但 ASR 上游(OpenAI兼容)偶发慢；超时后只报"暂时不可用"，**没有重试**，且错误信息丢失了上游 detail | `video_compose_asr.py:161,170` |
| 3 | canvas-agent-plan SSL EOF | `canvas_agent.py:98` 调 `core._post(timeout=120)`，走 `urllib.request.urlopen`（进程级 HTTPS_PROXY），**没有重试，单次 SSL 抖动直接失败** | `canvas_agent.py:98` + `core.py:1010-1017` |

## 修复方案（3 个独立改动，互不依赖）

### 修复 1：egress HTTPError 读响应体 + 区分 4xx

**文件**：`server/content_domains/egress.py`
**位置**：`post_json` 函数 196-207 行
**改什么**：当 HTTPError 非 429 且非"送达前失败"时，`raise` 之前读取 `e.read()` 拿到上游错误体，拼进异常 message，让日志和退点信息能看到上游真实原因（如 "API key not valid" / "model not found"）。

```python
# 现在（196-207行）：
if not _pre_delivery_failure(e):
    if log:
        log("[egress] %s via %s 失败，且请求可能已送达上游（换通道会重复计费），"
            "直接失败退点: %s" % (path, label, str(e)[:120]))
    raise

# 改为：读 HTTPError 响应体，拼进异常
if not _pre_delivery_failure(e):
    detail = ""
    if isinstance(e, urllib.error.HTTPError):
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
    if log:
        log("[egress] %s via %s 失败，且请求可能已送达上游（换通道会重复计费），"
            "直接失败退点: %s %s" % (path, label, str(e)[:120],
            ("| 上游响应: " + detail if detail else "")))
    raise urllib.error.HTTPError(
        e.filename, e.code, "%s: %s" % (e.reason, detail), e.headers, None
    ) if isinstance(e, urllib.error.HTTPError) else e
```

**为什么这样改**：不改降级逻辑（非幂等保护仍然在），只是让 400 的真实原因不再被吞掉。banana 的 400 大概率是 Gemini key/模型/配额问题，看到具体错误体才能对症。

### 修复 2：ASR 超时加 1 次重试 + 保留上游错误

**文件**：`server/content_domains/video_compose_asr.py`
**位置**：`transcribe` 函数 159-171 行
**改什么**：把单次 `opener.open` 包成"最多重试 2 次（指数退避 2s/4s）"，SSL/超时类失败重试，HTTPError(4xx) 不重试直接报。

```python
# 现在（160-161行）：
with _ASR_LOCK:
    with opener.open(request, timeout=max(120, int(duration * 5))) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))

# 改为：带 1 次重试
max_attempts = 2
for attempt in range(max_attempts):
    try:
        with _ASR_LOCK:
            with opener.open(request, timeout=max(120, int(duration * 5))) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        break
    except urllib.error.HTTPError:
        raise  # 4xx 不重试，走下面已有的 HTTPError 处理
    except Exception as retryable:
        if attempt + 1 >= max_attempts:
            raise AsrError("语音识别服务暂时不可用（已重试）") from retryable
        time.sleep(2 ** attempt)  # 2s, 4s
```

**为什么这样改**：ASR 超时多为上游偶发慢/SSL 抖动，重试 1 次大概率成。已有 `_ASR_LOCK` 保证不并发。不重试 HTTPError（如 401 key 错）避免无意义重试。

### 修复 3：canvas_agent _responses_chat 加 1 次重试

**文件**：`server/content_domains/canvas_agent.py`
**位置**：`_responses_chat` 函数 98 行
**改什么**：`_post(...)` 调用包成"最多 2 次"，SSL/超时类失败重试 1 次（间隔 2s）。

```python
# 现在（98行）：
response = _post("/v1/responses", json.dumps(request, ensure_ascii=False).encode(),
                 "application/json", base=API_BASE, key=API_KEY, timeout=120)

# 改为：SSL/超时重试 1 次
import time as _time
last_err = None
for attempt in range(2):
    try:
        response = _post("/v1/responses", json.dumps(request, ensure_ascii=False).encode(),
                         "application/json", base=API_BASE, key=API_KEY, timeout=120)
        break
    except Exception as e:
        last_err = e
        if attempt == 0 and isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError)):
            _time.sleep(2)
            continue
        raise
else:
    raise last_err
```

**为什么这样改**：canvas_agent 走的是 OpenAI Responses API（CANVAS_AGENT_API_BASE），SSL EOF 是网络层抖动不是业务错误，重试 1 次成本低、收益高。`store=False` 保证重试不会产生重复副作用。

## 不改什么（守住边界）
- **不改 egress 降级逻辑**：非幂等保护是命脉，HTTP 400 "可能已送达"的判断保持
- **不改 timeout 数值**：120s/210s 是经过验证的，不因偶发抖动放大
- **不改代理配置**：10810 在监听、egress 链路通，不是代理问题
- **不改 canvas_agent 的 model/base**：模型和 base 配置正确，是连接稳定性问题
- **banana 400 的根因**：修完修复1后看上游响应体再定（可能是 Gemini key 配额或模型名），本次只让错误可见

## 验证计划
1. 本地跑现有相关测试：`python -m pytest tests/ -k "egress or asr or canvas_agent" -x`
2. 提 PR，CI 全绿
3. 部署后重新测三个能力：
   - `image-generate`（banana）：看 400 是否给出具体上游错误
   - `collect-transcript`：看 ASR 是否重试后成功
   - `canvas-agent-plan`：看 SSL 是否重试后成功
4. 三个都重新报价+提交验证

## 风险
- 三处改动都是**加薄重试层 + 改善错误信息**，不改业务逻辑、不改计费、不改降级保护
- 重试有 `store=False`/`_ASR_LOCK`/幂等保证，不会重复计费或重复生成
- 最坏情况：重试也失败，行为退化为现状（失败退点），不会更差
