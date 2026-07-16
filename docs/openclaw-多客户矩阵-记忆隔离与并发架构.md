# OpenClaw 多客户矩阵：记忆隔离 + 并发 + 架构选型

> 研究时间：2026-06-22 ｜ 方法：只读分析 OpenClaw 本地源码 `/opt/homebrew/lib/node_modules/openclaw`（与服务器同款）+ 配置 `~/.openclaw/openclaw.json`
> 性质：**源码层结论已确证**（带文件:行号）；**服务器实盘验证未完成**（当次 SSH 链路受阻，见文末"待验证"）。
> ⚠️ 本文档只记录架构与机制，不含任何密钥 / 客户 PII。

---

## 0. 业务目标（这份研究服务于什么）

把【一个飞书群 + 3 个能力 bot】做成**可复制售卖的矩阵**：
1. 销售把这套卖出去 → 2. 客户拉进一个专属飞书群 → 3. 把 3 个 bot 拉进群。
3 个 bot = **(a) 文案 bot ｜ (b) 图片 bot ｜ (c) 爬取 bot**。要规模化服务很多个这样的客户群。

核心要回答两件事：① 不同客户**记忆会不会串**？② 多客户**同时用，并发扛不扛得住、权限怎么控**？

---

## 1. 记忆隔离：会串吗？根因在哪

### 一句话结论
**群与群的"当下对话"不会串（已隔离）；会串的是"长期记忆"**——同一个 bot(agent)进多个群时，它在 A 群被"记住"的东西沉淀进这个 bot **唯一的一本记忆**，B 群启动又把这本读进脑子。**根因：记忆按"哪个 bot(agentId)"存一份，不按"哪个群(chat_id)"存。**

### A) 会话上下文 —— 按群隔离，✅ 不会串
- 群消息会话钥匙：`agent:<agentId>:feishu:group:<群chat_id>`，带群 id。
  - 证据：`dist/session-key-BAP1m9Ju.js:117-124`（群消息 `return agent:${agentId}:${channel}:${peerKind}:${peerId}`，peerId=群 chat_id）。
- 实盘：`~/.openclaw/agents/main/sessions/sessions.json` 里 6 个不同群 = 6 个独立 session 文件，0 共享；逐个 .jsonl 扫描无串。

### B) 长期记忆 —— 按 bot 存一份，⚠️ 会串（根因）
- 记忆缓存键 = agentId：`dist/memory-Cs9GxIml.js:422`（`buildQmdManagerScopeKey(agentId){ return agentId }`）。
- 记忆库 = `~/.openclaw/memory/<agentId>.sqlite`（实盘仅 `main.sqlite` 一份）。
- 记忆笔记目录 = `workspace-<agentId>/`（`dist/agent-scope-config-jq9Xbh_R.js:110`）里的 `MEMORY.md` + `memory/*.md`。
- **致命一环 · dreaming（每日自动蒸馏）**：按某个群会话触发（`dist/dreaming-CGWXMiEI.js:114`），却把总结**写回到全 bot 共享的 `workspaceDir/memory/`**（`dist/dreaming-phases-Cyv7G2PG.js:49-55,83,94`）。→ A 群的总结进了公共本子，B 群开机经 `contextInjection:"always"` 无差别读到。

### 歪打正着的部分保护
- 默认记忆 scope `DEFAULT_QMD_SCOPE = {default:"deny", rules:[{allow, chatType:"direct"}]}`（`dist/backend-config-CbQX9WiD.js:32-38,241`）→ **群聊里"主动搜记忆"默认被拒**。但盖不住 dreaming 的"自动写" + `MEMORY.md` 的"开机注入"，所以仍会串。

### 私聊默认也会串（另一个独立问题）
- 配置 `session` 块缺失 → 私聊走默认 `dmScope:"main"`，**同一 bot 下所有人私聊塌缩进一个会话**，互相可见。改 `session.dmScope = "per-account-channel-peer"` 即可按人隔离（5 分钟改配置，不影响群）。

---

## 2. 并发：同时 @，回得过来吗

### 两道闸门
| 闸门 | 范围 | 上限 | 可调 |
|---|---|---|---|
| 群闸门(session lane) | 每个群各一条队列 | **1**（同群串行） | ❌ 写死 |
| 全局闸门(main lane) | 整个 bot 进程共用 | **默认 4** | ✅ 改 `agents.defaults.maxConcurrent` |

- 证据：全局默认 4 — `dist/agent-limits-Y6_vNNMs.js:1-4`；满了排队 — `dist/command-queue-Bu19cj-7.js:190`；每条消息双层套娃 `enqueueSession(()=>enqueueGlobal(...))` — `dist/pi-embedded-CJ87lW5R.js:1700-1702`。本地未设 maxConcurrent → 实际 = 4。

### 关键发现：出图不占闸门，爬取占
- **出图/出视频/出音乐**：工具一调用就把 90 秒生成甩到**后台**，毫秒级返回"已开始"，**不占那 4 个名额**。证据：`dist/openclaw-tools-C-Wfxs6l.js:3180-3187`（`queueMicrotask` 后台调度）、`:4331-4368`（立即 `return status:"started"`）。→ **A 群出图时 B 群 @ 图片 bot 几乎不用等；四群同时出图也不卡。**
- **爬取 / 语音转文字(ASR) / 长抓取**：当前任务里 `await` 等结果，**全程占 1 个名额好几分钟**。证据：`dist/selection-BmjEdnnA.js:5024`（`await toolParams.execute()`）。→ **4 个群同时爬取就占满，第 5 个群发"你好"也得干等。这才是"bot 像死了"的真凶。**

### 能扛多少
| 任务 | 占闸门 | 同进程能同时扛 |
|---|---|---|
| 文案问答(秒回) | 占但很快放 | 远超 4，几乎无感 |
| 出图/视频/音乐 | **不占(后台)** | 基本无上限(受 API 配额) |
| 爬取/ASR | **占满几分钟** ⚠️ | **只有 4** |
| 同群内连发 | — | **永远 1(串行)** |

> 硬上限 = 每进程 **4 个"前台长任务"**（爬取类），出图不算。这个 4 是**每进程/每 home 实例**算的。挂 50 个客户群不一定卡，只要不是 5 个群同一秒触发重活。

---

## 3. 权限 / 隔离

- **防白嫖**：✅ 机制有（按群 chat_id 白名单 + 按人白名单，`dist/policy-CtPXhkC9.js:95-110`；allowlist 为空则默认拒绝 `dist/runtime-CBn7_ZUF.js:166-170`）。❗**但本地 `default` 账号被设成 `groupPolicy="open"`（任谁拉进群都接待 = 可白嫖）**，运行期 policy 缺省也会退化成 open（`dist/runtime-group-policy-CmZDlIwd.js:26-34`）。**P0：改回 allowlist + 写客户群白名单。**（服务器 3 套配置需另行核对。）
- **限流**：同群串行 + 全局 4 闸门即粗限流；同群消息默认 `debounce 500ms`、队列 `cap=20`，超出按 summarize 丢弃（`dist/queue-CwwXydpR.js:656-659`）。高峰群要评估是否调大 cap。
- **记忆隔离(红线)**：见第 1 节。卖给不同客户**必须按客户换 agentId 或换 home**，否则数据串台。
- **多 bot 同群**：✅ 各认各的 @，互不触发打架——"一群 3 bot"天然支持。

---

## 4. 方案一 vs 方案二

- **方案一**：所有客户群挂同一个 home / 同一个 node 进程，靠 agentId 区分 bot。
- **方案二**：每个客户(或每档)一套独立 home 实例、独立进程、独立端口（服务器现在 `.openclaw / .openclaw-second / .openclaw-visual` 就是这路子）。

| 维度 | 方案一·共享进程多 agent | 方案二·按客户拆 home/进程 |
|---|---|---|
| 并发 | 全客户共抢 1 个 4 闸门，一个客户爬取拖慢所有人 | 每实例独立 4 闸门，N 实例≈4×N，互不拖 |
| 记忆隔离 | ❌ 同 agentId 服务多群会串 | ✅ 天然硬隔离(独立 sqlite + MEMORY.md) |
| 飞书 App | 3 个，全客户复用 | **同样 3 个**(App 与进程无关，靠拉群授权) |
| 进程/内存 | 省(1 进程 ~50–130MB) | 费(每实例 +50–130MB，长任务峰值更高) |
| 加客户成本 | 改配置加 binding，近零边际 | 起新实例，线性增长 |
| 适合 | 客户少、轻任务、能接受偶发排队 | 客户多、有重任务、要强隔离 |

> **关键澄清：3 个飞书 App 按"能力 bot"算，不按"客户"算。** 卖 100 个客户也还是这 3 个 App。飞书 App 不是瓶颈。

---

## 5. 推荐架构（可转述给大鹏老板）

**混合架构：能力 bot 全局共享 + 按客户分片隔离 + 并发靠多 worker 实例摊开。**

> 老板版一段话：
> "我们做一套标准的『3 能力 bot（文案/图片/爬取）』，只占 3 个飞书 App，所有客户复用、不随客户增加。客户买单后把这 3 个 bot 拉进他的专属群即可开通——每个客户群消息天然独立、互不串。后台把客户按批分片到多个隔离实例（每 8–10 个客户群一个实例），既保证记忆不串台、数据合规，又让并发随实例数线性扩展。文案秒回、出图后台异步不卡人，只有爬取这类重任务受单实例限制，靠『分片 + 调高并发参数 + 重任务异步化』三招解决。一次搭好、按客户复制开通的可售卖矩阵。"

落地三原则：① 能力 bot 共享（3 App 固定）② 记忆按客户隔离（不同 home/agentId，红线）③ 并发靠多实例摊开 + 把爬取改成"先回执后台跑"。

---

## 6. 服务器配置建议

> 更正：实测服务器是 **8G 内存 / 4 核**（不是早前架构文档写的 3.6G），当次故障时仅用 48%、CPU 23%——**没有 OOM，机器健康**。所以"加 swap/升内存"不紧急。

| 阶段 | 配置 | 说明 |
|---|---|---|
| 现状 | 8 核? / **8G** / 59G | 几个客户够用 |
| 真正起步生产 | **8 核 / 16G / 100–200G SSD** | 甜点；腾讯云可关机原地升配 |
| 规模化(50–100+客户) | **拆机**：爬取单独一台 + bot 实例横向多台 | 见下 |

吃内存大户是**爬取**（用 Playwright/无头 Chrome，一个浏览器几百兆），规模化时应：**A 机**跑 bot 实例(文案/图片/对话)，**B 机**专门跑爬取(MediaCrawler+号池+代理，可随时重启、与客户隔离)。

⚠️ **真正的天花板可能在中转 API**：LLM、出图全走 `api.zelong.vip` 等外部 API，**其并发/RPM 上限决定能同时服务多少客户，加服务器也绕不过**——比升硬件更该先压测。

---

## 7. 下一步可执行清单

- **P0 防白嫖**：核对服务器 3 套 `openclaw.json` 的 `groupPolicy`，把 `default` 从 `open` 改回 `allowlist` + 写客户群 chat_id 白名单。
- **P0 验记忆串台**：两个客户群同 agentId 跑几轮 + 触发一次 dreaming，确认串台、锁死"必须按客户隔离"。
- **P1 压测并发**：本地搭"3 bot 进 1 群"样板，模拟 5+ 群同时爬取，实测第 5 群等待时间，验证 4 闸门 + 调高 maxConcurrent 效果。
- **P1 重任务异步化**：调研爬取/ASR 能否像出图那样后台 detached（缓解卡顿的最大杠杆，不加机器）。
- **P2 分片模板**：写"一键开客户"脚本（起 home + 拉 3 bot 进群 + 写白名单）。
- **P2 压测中转 API**：测 `api.zelong.vip` / 中转的并发与 RPM 上限。
- **私聊串修复**：`session.dmScope = "per-account-channel-peer"`（不影响群）。

---

## 8. 待验证（诚实标注，不能拍脑袋）

| 不确定点 | 现状 | 怎么定 |
|---|---|---|
| 服务器 3 套实例的真实 `maxConcurrent` / `groupPolicy` | 本地确证；服务器未核对(SSH 受阻) | 在控制台读 3 份 openclaw.json |
| dreaming 一次读"全部群"还是"只读触发群" | 已确证写回共享 memory 目录；corpus 选取范围未逐行确认 | 追 dreaming 的 session 读取范围 |
| `memory.qmd.scope` 能否拦 dreaming 的"写" | 默认只确认拦"搜索读" | 实测 |
| 爬取/ASR 能否异步化 | 出图已异步；爬取倾向同步阻塞 | 读 cpa-bot/researcher 工具实现 |
| 中转 API 并发/RPM/额度 | 完全未知，可能是真天花板 | 单独压测 |
| 单进程能稳开几个 home 实例 | 空载 ~50–130MB/进程；长任务峰值 RSS 未测 | 跑长任务实测峰值 RSS |

---

## 附：源码证据文件清单（本地绝对路径）

- 会话 key：`/opt/homebrew/lib/node_modules/openclaw/dist/session-key-BAP1m9Ju.js`
- 记忆按 agentId：`.../dist/memory-Cs9GxIml.js` + `agent-scope-config-jq9Xbh_R.js`
- dreaming 写共享记忆：`.../dist/dreaming-phases-Cyv7G2PG.js` + 触发 `dreaming-CGWXMiEI.js`
- 默认记忆 scope：`.../dist/backend-config-CbQX9WiD.js`
- 并发闸门：`.../dist/agent-limits-Y6_vNNMs.js`、`command-queue-Bu19cj-7.js`、`pi-embedded-CJ87lW5R.js`
- 出图后台异步：`.../dist/openclaw-tools-C-Wfxs6l.js`
- 爬取同步占槽：`.../dist/selection-BmjEdnnA.js`
- 防白嫖/policy：`.../dist/policy-CtPXhkC9.js`、`runtime-group-policy-CmZDlIwd.js`
- 本地配置真相源：`~/.openclaw/openclaw.json`

---

_相关文档：[`openclaw-agents架构.md`](./openclaw-agents架构.md)（广州服务器 3 实例/4 子 agent/4 飞书 App 盘点）。_
