# BUG-0003 Nginx 安全响应头设计

## 目标

为旧主服务器 `129.204.166.13` 的 HTTPS 响应补齐 HSTS、点击劫持保护、MIME 嗅探保护和 Referrer 策略，并隐藏 Nginx 精确版本。修复覆盖 HTML、静态资源和反向代理 API，不涉及新服务器或业务代码。

## 方案

在 HTTPS `server` 中启用 `server_tokens off`，并声明：

- `Strict-Transport-Security: max-age=31536000`
- `X-Frame-Options: DENY`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`

CSP 增加 `frame-ancestors 'none'`，与 `X-Frame-Options: DENY` 形成现代及旧客户端兼容保护。

Nginx 1.18 中，location 只要声明自己的 `add_header`，就不会继承 server 级响应头。因此在根路径、通用静态页面和带缓存头的静态资源 location 中显式重复完整安全头。API location 没有自己的 `add_header`，继续继承 server 级安全头。

HTTP 80 server 同样启用 `server_tokens off`；HSTS 只在 HTTPS 响应发送。`server_tokens off` 隐藏 `nginx/1.18.0 (Ubuntu)`，保留通用 `Server: nginx`。完全删除 Server 头需要额外模块，不纳入本 PR。

## 文件与验证

- 修改 `deploy/nginx-huangquechuanmei.conf` 作为部署正本。
- 同步修改 `server/nginx-huangquechuanmei.conf`，避免恢复手册引用的两份配置继续产生安全差异。
- 扩展 `tests/test_nginx_csp.py`，验证两份配置、安全头取值、CSP frame-ancestors、location 继承边界和版本隐藏。
- 不部署、不 reload Nginx；合并后按手册执行 `nginx -t`、reload，再对 HTML、静态资源和 API 回归响应头。

## 验收

- 自动化测试在修改前失败、修改后通过。
- 两份配置均启用 `server_tokens off`。
- HTTPS server 与三个自定义 add_header location 都包含完整安全头。
- CSP 全部包含 `frame-ancestors 'none'`。
- PR 不安装模块、不修改业务代码、不包含生产凭据。
