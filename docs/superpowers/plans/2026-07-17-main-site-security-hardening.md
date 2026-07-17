# Main Site Security Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `LU-003/huangque-test-server` 中完成第一批安全加固，阻断公网内部接口、为 Cookie 状态变更请求增加 CSRF/来源校验、限制管理后台入口并补齐回归测试，然后仅部署到 `8.138.143.64` 验证。

**Architecture:** 认证服务负责 Session 绑定的签名 CSRF Token、严格 Origin/Referer 校验和敏感操作业务校验；浏览器端统一为同源状态变更请求附加 CSRF 请求头；Nginx 在公网边界精确隐藏内部接口、清空伪造内部令牌、限制管理后台来源并添加安全响应头。内部服务仍通过 `127.0.0.1:8095` 与认证服务通信，不改变 SQLite 结构和现有点数事务。

**Tech Stack:** Python 3 标准库 HTTP 服务、SQLite、原生 JavaScript、Nginx、systemd、Python `unittest`、Node.js 前端测试、Shell 部署校验。

## Global Constraints

- 只修改 `LU-003/huangque-test-server` 的 `codex/main-site-security-hardening` 分支；本计划不修改 `tang730125633/huangque-main-site` 或 `129.204.166.13`。
- 先写失败测试，再写最小实现；每个任务独立提交，便于未来逐个 cherry-pick 到主站最新 `main`。
- 第一批不改数据库结构、不复制生产数据库或生产密钥、不迁移密码算法、不改价格表。
- 内部服务调用保持 `127.0.0.1:8095`；浏览器和公网永远不能获得或提交有效 `X-HQ-Internal-Token`。
- CSRF Token 不写 URL、日志或数据库；使用服务端密钥对当前 Session token 做 HMAC，采用常量时间比较。
- Cookie 请求执行 CSRF + Origin/Referer；明确使用 Bearer Token 的小程序/服务端调用不执行浏览器 CSRF，但仍执行认证和 JSON 校验；微信支付通知只走现有 V3 签名校验。
- 管理 IP 白名单必须在部署前由操作者写入测试服务器的 Nginx include 文件；仓库只提交示例，不提交个人公网 IP。
- 每个实现提交前运行目标测试和 `git diff --check`；全部完成后运行完整 Python/Node 测试及 Nginx 配置测试。

## File Map

- `server/auth_server.py`：Session、CSRF Token、Origin/Referer、JSON 内容类型、管理员调点及充值审批的最终校验。
- `server/admin_api.py`：管理 API 入口的浏览器请求校验、请求 ID 透传、调点阈值与原因的第一层校验。
- `site/workbench/cloud-shell.js`：全站同源请求 helper、CSRF Cookie 读取和状态变更请求头注入。
- `site/login.html`：独立登录页与注册页的 CSRF Cookie 生命周期兼容。
- `site/admin/index.html`：管理后台状态变更请求的 CSRF 请求头、原因输入及大额调点确认。
- `deploy/test-server/nginx.conf`：测试环境公网内部端点阻断、内部头清理、管理入口白名单、安全响应头。
- `deploy/nginx-huangquechuanmei.conf`：未来主站适配所用的 HTTPS Nginx 模板；与测试配置保持同一安全边界。
- `deploy/test-server/admin-allowlist.conf.example`：管理入口白名单格式示例，不含真实地址。
- `deploy/test-server/verify-full-environment.sh`：部署后内部/公网边界、服务健康和安全响应头冒烟检查。
- `tests/test_auth_csrf.py`：认证服务 CSRF、来源、Bearer、支付回调和 JSON 例外测试。
- `tests/test_admin_security.py`：管理员角色、原因、阈值、请求 ID 和余额不变测试。
- `tests/test_nginx_security_boundary.py`：Nginx 精确拒绝、内部头清理、白名单和安全响应头静态测试。
- `tests/test_cloud_shell_csrf.js`：前端同源状态变更请求头及跨域不泄漏测试。
- `tests/test_security_regression.py`：注册、登录、退出、资料、充值、内部扣点/退款的整条回归。
- `docs/security/test-server-security-runbook.md`：测试部署、验证、日志检查和回滚手册。

---

### Task 1: 建立 Session 绑定的 CSRF 基础能力

**Files:**
- Create: `tests/test_auth_csrf.py`
- Modify: `server/auth_server.py`

**Interfaces:**
- `csrf_token_for(session_token: str) -> str`
- `csrf_cookie_header(session_token: str) -> str`
- `clear_csrf_cookie_header() -> str`
- `H._csrf_valid(session_token: str) -> bool`
- 新环境变量：`HQ_CSRF_SECRET`；生产/测试部署必须显式设置且不得写入仓库。

- [ ] 编写测试：相同 Session 产生稳定 token，不同 Session 产生不同 token；空 Session/空密钥拒绝；错误 token 使用 403；比较使用 `secrets.compare_digest` 可被行为测试覆盖。
- [ ] 编写 HTTP 测试：登录和注册响应同时设置 HttpOnly `hq_session` 与可供同源 JS 读取的 `hq_csrf` Cookie；CSRF Cookie 必须包含 `SameSite=Lax; Path=/`，HTTPS 模式包含 `Secure`，但不得包含 `HttpOnly`。
- [ ] 运行 `python -m unittest tests.test_auth_csrf -v`，确认因 helper/Cookie 尚不存在而失败。
- [ ] 在 `server/auth_server.py` 使用 `hmac.new(CSRF_SECRET, session_token.encode('utf-8'), hashlib.sha256).hexdigest()` 实现 token；空密钥时启动失败，测试通过显式注入固定测试密钥。
- [ ] 扩展 `_send` 使单个响应可安全发出两个 `Set-Cookie` 头，登录/注册设置两个 Cookie，退出同时清除两个 Cookie；不得用逗号拼接 Cookie。
- [ ] 运行 `python -m unittest tests.test_auth_csrf tests.test_auth_points -v`，预期全部通过。
- [ ] 运行 `git diff --check`，提交：`security: bind csrf tokens to auth sessions`。

### Task 2: 对 Cookie 状态变更统一执行来源、CSRF 和 JSON 校验

**Files:**
- Modify: `tests/test_auth_csrf.py`
- Modify: `server/auth_server.py`
- Create: `tests/test_security_regression.py`

**Interfaces:**
- `H._uses_bearer_auth() -> bool`
- `H._request_origin_allowed() -> bool`
- `H._require_browser_mutation_security(path: str) -> bool`
- 新环境变量：`HQ_ALLOWED_ORIGINS`，逗号分隔的完整 Origin（协议、主机、可选端口）。
- 明确例外：`/api/auth/login`、`/api/auth/register` 只要求 JSON 和合法 Origin；`/api/auth/miniprogram-*` Bearer 流程只要求 JSON；`/api/auth/wxpay/notify` 使用微信签名且不要求 CSRF；文件上传端点保持自身 multipart 校验。

- [ ] 添加失败测试：Cookie Session 的 `POST/PUT/PATCH/DELETE` 缺失或错误 `X-CSRF-Token` 返回 403；恶意 Origin、伪造子域、`Origin: null` 和不匹配 Referer 返回 403。
- [ ] 添加失败测试：合法 Origin + 合法 Token 允许退出、资料修改、密码修改、充值下单；非 JSON 浏览器 mutation 返回 415。
- [ ] 添加回归测试：`Authorization: Bearer` 小程序流程不因缺少浏览器 CSRF 失败；微信通知仍进入签名验证分支；GET/HEAD 不要求 CSRF。
- [ ] 运行 `python -m unittest tests.test_auth_csrf tests.test_security_regression -v`，确认新安全断言失败。
- [ ] 在 `H.do_POST` 进入路由分发前执行统一 guard；精确解析 Origin，不做字符串后缀匹配；缺 Origin 时仅接受同源 Referer；两者都缺失的 Cookie mutation 默认拒绝。
- [ ] 让登录/注册在校验合法 Origin 后工作，并保留命令行/测试兼容的显式配置；Bearer 和支付回调只进入列出的例外，不建立通配例外。
- [ ] 记录安全拒绝事件时仅写 `request_id/path/client_ip/origin/reason`，不写 Cookie、CSRF 或 Authorization 值。
- [ ] 运行 `python -m unittest tests.test_auth_csrf tests.test_security_regression tests.test_auth_points tests.test_auth_profile -v`，预期全部通过。
- [ ] 运行 `git diff --check`，提交：`security: enforce origin csrf and json mutations`。

### Task 3: 前端统一附加 CSRF 请求头且不向跨域泄漏

**Files:**
- Create: `tests/test_cloud_shell_csrf.js`
- Modify: `site/workbench/cloud-shell.js`
- Modify: `site/login.html`
- Modify: `site/admin/index.html`

**Interfaces:**
- `HQ.csrfToken() -> string`
- `HQ.secureFetch(input, init) -> Promise<Response>`
- `secureFetch` 仅对同源 `POST/PUT/PATCH/DELETE` 添加 `X-CSRF-Token`；GET、跨域 URL、Bearer 小程序请求均不添加。

- [ ] 添加 Node 测试：从 `hq_csrf` Cookie 正确解码；同源 mutation 自动设置请求头；已有请求头不被错误覆盖；跨域 URL 永不携带 token；GET 不添加 token。
- [ ] 添加静态测试：`cloud-shell.js` 中登录、注册、退出和用户资料 mutation 使用 `secureFetch`；`site/admin/index.html` 的管理 mutation 使用同一规则。
- [ ] 运行 `node --test tests/test_cloud_shell_csrf.js`，确认 helper 尚不存在而失败。
- [ ] 在 `cloud-shell.js` 实现 cookie 解析与 `secureFetch`，通过 `new URL(input, location.href).origin === location.origin` 做同源判断，不 monkey-patch 全局 `window.fetch`。
- [ ] 登录/注册成功后直接使用服务端设置的 CSRF Cookie；退出请求发送 header，响应后清理 UI 状态。独立 `site/login.html` 使用同一最小逻辑。
- [ ] 在 `site/admin/index.html` 的管理 POST 调用中统一注入 header；管理员 token 继续按现有 Cookie/Bearer 机制传递，不放 URL。
- [ ] 运行 `node --test tests/test_cloud_shell_csrf.js tests/test_cloud_shell_sidebar.js`，预期通过；再运行相关 Python UI 静态测试。
- [ ] 运行 `git diff --check`，提交：`security: send csrf tokens from browser clients`。

### Task 4: 加固管理员调点与充值审批

**Files:**
- Create: `tests/test_admin_security.py`
- Modify: `server/admin_api.py`
- Modify: `server/auth_server.py`
- Modify: `site/admin/index.html`

**Interfaces:**
- `validate_admin_reason(value: object) -> str`：去除首尾空白后 4～120 字符。
- `validate_points_delta(value: object) -> int`：非零整数且 `abs(delta) <= HQ_ADMIN_POINTS_MAX_DELTA`。
- 新环境变量：`HQ_ADMIN_POINTS_MAX_DELTA`，测试环境初始值 `1000`。
- 请求头：`X-Request-ID`；若客户端未提供，由入口生成，认证服务与点数审计记录使用同一 ID。

- [ ] 添加失败测试：非管理员不能调点/审批；缺失、过短、过长原因返回 400；0、非整数和超过阈值的 delta 返回 400；所有拒绝均保持余额和审计行数不变。
- [ ] 添加成功测试：合法调点记录管理员、目标用户、前后余额、原因和请求 ID；充值 approve/reject 必须填写合格原因；重复审批仍保持幂等。
- [ ] 运行 `python -m unittest tests.test_admin_security tests.test_points_audit -v`，确认原因/阈值/请求 ID 断言失败。
- [ ] 在 `admin_api.py` 做入口校验并将 request ID 透传到 `auth_admin_request`；在 `auth_server.py` 再次执行相同最终校验，不能只信任管理员服务。
- [ ] 在现有 `points_audit` 的 metadata/reason 字段保存 request ID，不新增数据库列；限制日志中不出现 Session 或内部 token。
- [ ] 在 `site/admin/index.html` 将原因设为必填，显示 4～120 字限制；超过阈值的按钮在前端禁用但服务端仍是最终裁决者。
- [ ] 运行 `python -m unittest tests.test_admin_security tests.test_points_audit tests.test_auth_points -v`，预期通过。
- [ ] 运行 `git diff --check`，提交：`security: constrain and audit admin point changes`。

### Task 5: 在 Nginx 公网边界隐藏内部接口并清空伪造内部令牌

**Files:**
- Create: `tests/test_nginx_security_boundary.py`
- Modify: `deploy/test-server/nginx.conf`
- Modify: `deploy/nginx-huangquechuanmei.conf`

**Interfaces:**
- 精确返回 404 的公网路径：`/api/auth/points/deduct`、`/api/auth/points/refund`、`/api/auth/admin/points/adjust`、`/api/auth/admin/points/audit`、`/api/auth/admin/users`、`/api/auth/admin/recharge/review`、`/api/auth/admin/recharge/orders`。
- 通用 `/api/auth/` 代理必须包含 `proxy_set_header X-HQ-Internal-Token "";`。

- [ ] 添加静态失败测试：两份 Nginx 模板均在通用 auth location 之前含 7 个 `location = ... { return 404; }`；禁止使用会误伤公开 auth 路由的宽泛正则。
- [ ] 添加静态失败测试：公网 auth 代理清空 `X-HQ-Internal-Token`，管理 API 不会把浏览器伪造的内部 token 原样转发。
- [ ] 运行 `python -m unittest tests.test_nginx_security_boundary -v`，确认规则缺失而失败。
- [ ] 在两份模板添加精确拒绝规则与 header 清理；保持内部服务的 `127.0.0.1:8095` 调用代码不变。
- [ ] 运行 `python -m unittest tests.test_nginx_security_boundary tests.test_upstream_guard -v`，预期通过。
- [ ] 在可用 Nginx 环境运行 `nginx -t -c <临时拼装的绝对配置路径>`，预期 `syntax is ok` 和 `test is successful`。
- [ ] 运行 `git diff --check`，提交：`security: close public internal auth routes`。

### Task 6: 限制管理后台网络入口并补齐安全响应头

**Files:**
- Modify: `tests/test_nginx_security_boundary.py`
- Modify: `tests/test_nginx_csp.py`
- Create: `deploy/test-server/admin-allowlist.conf.example`
- Modify: `deploy/test-server/nginx.conf`
- Modify: `deploy/nginx-huangquechuanmei.conf`

**Interfaces:**
- 测试服务器实际文件：`/etc/nginx/snippets/huangque-admin-allowlist.conf`，内容为一个或多个 `allow <CIDR>;`，最后一行 `deny all;`。
- 安全响应头：HSTS（仅 HTTPS 模板）、`X-Content-Type-Options`、`Referrer-Policy`、`Permissions-Policy`、CSP `frame-ancestors 'self'`。

- [ ] 添加失败测试：`/admin/` 和 `/api/admin/` 都 include 同一白名单；示例以 `deny all` 结尾；HTTPS 模板含 HSTS；两份模板含其余响应头和 `frame-ancestors`。
- [ ] 更新 CSP 测试，明确保留现有 `unsafe-inline`，防止本批次误删导致页面不可用。
- [ ] 运行 `python -m unittest tests.test_nginx_security_boundary tests.test_nginx_csp -v`，确认白名单/响应头断言失败。
- [ ] 添加 include 和响应头；测试 HTTP 配置不伪造 HSTS，HTTPS 模板才启用 HSTS；静态资源 location 使用 `always` 保持关键响应头不被覆盖。
- [ ] 运行目标测试并执行 Nginx 语法检查，预期全部通过。
- [ ] 人工检查：白名单文件未包含真实个人 IP、仓库未出现证书私钥或生产域名密钥。
- [ ] 运行 `git diff --check`，提交：`security: restrict admin ingress and harden headers`。

### Task 7: 扩展部署校验与回滚手册

**Files:**
- Modify: `deploy/test-server/verify-full-environment.sh`
- Create: `docs/security/test-server-security-runbook.md`
- Modify: `docs/superpowers/specs/2026-07-17-main-site-security-hardening-design.md`（仅在实现与设计出现已确认偏差时更新）

**Interfaces:**
- 校验脚本参数：`PUBLIC_BASE_URL`、`AUTH_INTERNAL_URL=http://127.0.0.1:8095`、`ADMIN_ALLOWED_SOURCE`；密钥只从环境读取，不打印。
- 退出码非零表示任何安全边界或核心服务检查失败。

- [ ] 先在 shell 测试或静态测试中断言脚本覆盖：服务健康、公开登录页、7 个内部端点公网 404、伪造内部头仍 404、安全响应头、非白名单管理入口 403。
- [ ] 运行现有校验测试，确认新增断言失败。
- [ ] 扩展脚本：使用临时请求体和 `curl --fail-with-body`，输出路径与状态码但对 Cookie、CSRF、内部 token 做遮蔽。
- [ ] 编写 runbook：部署前备份 DB/Nginx/systemd/env，配置 `HQ_CSRF_SECRET`、`HQ_ALLOWED_ORIGINS`、阈值和白名单，代码/前端先部署、Nginx 最后 reload。
- [ ] 在 runbook 写明回滚顺序：先恢复 Nginx 并 reload，再切回上一代码 SHA，重启 auth/admin，最后做登录、余额和生成冒烟；本批次无 DB schema rollback。
- [ ] 运行文档/脚本静态测试与 `bash -n deploy/test-server/verify-full-environment.sh`，预期通过。
- [ ] 运行 `git diff --check`，提交：`docs: add security deployment and rollback runbook`。

### Task 8: 完整回归、自审与测试服务器部署验证

**Files:**
- Verify only: all files above
- Runtime only (不提交): `/etc/huangque/*.env`、`/etc/nginx/snippets/huangque-admin-allowlist.conf`、`/etc/nginx/sites-enabled/*`

**Interfaces:**
- 部署目标仅 `root@8.138.143.64:/opt/huangque-test-server`。
- 不推送、登录或修改 `129.204.166.13`。

- [ ] 运行目标安全套件：`python -m unittest tests.test_auth_csrf tests.test_admin_security tests.test_nginx_security_boundary tests.test_security_regression -v`，预期 0 failure/error。
- [ ] 运行完整 Python 套件：`python -m unittest discover -s tests -p 'test_*.py' -v`，保存总数和结果。
- [ ] 运行全部 Node 测试：`node --test tests/*.js`，预期 0 failure。
- [ ] 执行占位符扫描：`git grep -n -E 'TODO|FIXME|YOUR_IP|CHANGE_ME|csrf-secret-here' -- server site deploy tests docs/security`；只允许文档中明确说明的示例占位，运行配置不得保留占位。
- [ ] 执行秘密扫描：确认差异中不存在 `HQ_INTERNAL_TOKEN` 值、Session、CSRF secret、API key、证书私钥；执行 `git diff --check`。
- [ ] 自审设计覆盖：逐项核对 7 个内部端点、CSRF/Origin/JSON 例外、管理员原因和阈值、白名单、响应头、测试、回滚；检查 Python/JS 中 CSRF header 名称完全一致。
- [ ] 记录测试服务器当前 SHA，备份 `/opt/huangque-test-server/server/*.db`、Nginx site 配置和相关 systemd/env 文件；确认备份路径和时间戳。
- [ ] 在测试服务器写入独立 `HQ_CSRF_SECRET`、精确 `HQ_ALLOWED_ORIGINS=http://8.138.143.64`（启用 HTTPS 后再追加 HTTPS Origin）、调点阈值与管理员白名单；权限设为 root 可读。
- [ ] 部署代码并先重启 auth/admin，验证登录可取得 CSRF Cookie、合法 mutation 成功、非法 mutation 为 403；随后 `nginx -t`，最后 reload Nginx。
- [ ] 从公网验证 7 个内部接口均为 404、伪造内部 token 无效、非白名单管理入口为 403；从允许来源验证管理后台仍可登录和执行小额调点。
- [ ] 执行注册、登录、退出、资料、充值、图片、视频、内容、获客、扣点、退款和并发余额冒烟；观察 auth/admin/Nginx 日志，确认无 token/密钥泄漏。
- [ ] 连续观察 24～48 小时的 401/403/415/5xx、登录成功率、生成任务和余额审计；发现回归按 runbook 回滚，不带病合并主站。
- [ ] 所有验证通过后提交仅包含必要的测试结果记录（若仓库已有约定位置），保持工作树干净；再决定是否推送分支并创建 PR。

## Final Review Checklist

- [ ] 计划内每项实现都有先失败、后通过的测试证据。
- [ ] 所有浏览器 mutation 的 CSRF header 名统一为 `X-CSRF-Token`，后端与前端类型/大小写一致。
- [ ] 所有例外均为精确路径或明确 Bearer 条件，没有通配放行。
- [ ] 公网 Nginx 不转发 `X-HQ-Internal-Token`，7 个内部路由在通用代理之前返回 404。
- [ ] 调点失败路径不改变余额、不新增审计；成功路径含管理员、前后余额、原因、请求 ID。
- [ ] 代码、配置、文档没有 TODO、真实 IP、生产用户数据或明文秘密。
- [ ] 完整 Python/Node/Nginx/部署冒烟结果已记录，工作树干净。
- [ ] 只部署到 `8.138.143.64`；主站仓库和生产服务器保持未修改。
