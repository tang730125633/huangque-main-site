# 黄雀测试服务器 Nginx 与基础后端部署设计

## 目标

在 `8.138.143.64` 上把 `/opt/huangque-test-server` 部署为独立开发测试环境。第一阶段支持网站访问、注册登录、用户信息、基础内容 API 健康检查和基础管理 API；不启用需要第三方付费密钥的 AI、视频、COS 与采集能力。

## 部署边界

- 只修改测试服务器，不修改主站服务器。
- 不复制主站数据库、用户数据、API 密钥或支付配置。
- 使用 HTTP 地址 `http://8.138.143.64`；在分配测试域名之前不配置 HTTPS。
- 业务进程使用 `admin` 账户运行，不使用 root。

## 架构

- Nginx 监听公网 80 端口，静态根目录为 `/opt/huangque-test-server/site`。
- `/api/auth/` 反向代理到 `127.0.0.1:8095`。
- `/api/gen/` 反向代理到 `127.0.0.1:8096`。
- `/api/admin/` 反向代理到 `127.0.0.1:8098`。
- 其他页面使用仓库现有的无扩展名 URL 规则；根路径跳转至 `/workbench/inspiration`。
- 后端仅监听回环地址，公网不能绕过 Nginx 直接访问。

## 服务与数据

- `huangque-test-auth.service` 从仓库的 `server/auth_server.py` 启动认证服务。
- `huangque-test-content.service` 从仓库的 `server/content_api.py` 启动内容服务。
- `huangque-test-admin.service` 从仓库的 `server/admin_api.py` 启动管理服务。
- systemd 负责开机启动、异常重启和日志收集。
- 认证库、任务库和生成文件均为测试机独立数据。仓库已经忽略运行时数据库文件时，服务可直接在测试代码目录创建；否则在实施前增加忽略规则或使用独立运行目录，避免污染 Git 状态。
- `/etc/huangque-test/test.env` 保存测试专用环境变量，权限设为 root 可读、业务进程可通过 systemd 加载。

## 安全设置

- 生成测试机专用 `HQ_INTERNAL_TOKEN`，不沿用生产值。
- HTTP 阶段设置 `HQ_AUTH_COOKIE_SECURE=0`，使登录 Cookie 能在测试 IP 上工作。
- Cookie 保持 HttpOnly 与现有 SameSite 行为。
- 不配置微信支付、OpenAI/Gemini、COS、采集平台等生产凭据。
- Nginx 限制请求体尺寸并传递真实客户端地址；仅开放 80 端口，后端端口保持本机访问。

## 失败行为

- 未配置的 AI、视频、COS 或采集能力返回仓库已有的“未配置”错误，不应导致认证和基础页面不可用。
- 任一后端不可用时 Nginx 返回 502，并可通过 `journalctl` 和 Nginx 错误日志定位。
- Nginx 配置必须先通过 `nginx -t` 才能重载。

## 验收标准

1. `http://8.138.143.64` 能打开并跳转到工作台。
2. `/api/auth/health` 返回成功。
3. 新用户可以注册、登录并读取 `/api/auth/me`。
4. `/api/gen/health` 返回成功，且未配置的付费能力给出可理解的错误。
5. 三个 systemd 服务与 Nginx 均为 active，并设置为开机启动。
6. 服务器重启或单独重启服务后，网站与健康检查能恢复。
7. Git 工作区不包含测试数据库、日志或密钥。

## 后续扩展

获得独立测试域名后，可使用 Let's Encrypt 配置 HTTPS 并恢复 Secure Cookie。第三方能力应逐项加入独立测试密钥并单独验收，不能直接复制主站生产密钥。
