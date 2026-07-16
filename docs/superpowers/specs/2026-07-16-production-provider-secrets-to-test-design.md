# 主站供应商密钥复制到测试机设计

## 目标

把主站已配置的模型、采集、代理和腾讯云 COS 供应商配置复制到 `8.138.143.64`，使测试环境能够真实调用外部能力。测试机继续使用独立用户数据库和独立站内认证密钥。

## 风险接受

- 测试调用与主站共用第三方账户额度、配额和费用。
- 测试产出会写入主站 COS 存储桶，可能与生产对象混合。
- 供应商密钥在任一服务器泄露都会同时影响主站与测试站。
- 测试机将复用主站的 xray Reality 出口节点凭据；测试流量会占用同一出口节点，节点凭据泄露会同时影响两套环境。
- 用户已选择完整复制并接受上述边界。

## 复制范围

从主站 `/home/ubuntu/content-api/content.env` 复制模型、采集、代理、端点和 COS 变量，包括 Gemini、OpenAI、火山方舟、DashScope、TikHub、HeyGen、WaveSpeed、xAI、小乐视频、泽龙以及 COS 配置。

从 `/etc/huangque/runninghub.env` 复制 `RUNNINGHUB_API_KEY`，从 `/etc/leadgen-secrets.env` 复制 `TIKHUB_KEY`。

主站全部代理变量均指向 `127.0.0.1`。因此同时复制 `/usr/local/bin/xray-egress` 与 `/home/ubuntu/egress/xray-client.json`，在测试机启动独立的本地 xray HTTP 出口 `127.0.0.1:10809`。该隧道使用 VLESS/Reality 节点配置，不复制 SSH 私钥。

复制采用明确的变量名前缀和变量名白名单，不整文件盲拷。

## 明确排除

- 不复制 `/home/ubuntu/auth-service/auth.env`。
- 不复制 `HQ_INTERNAL_TOKEN`，测试机保留自己的内部认证令牌。
- 不复制微信公众平台或微信支付变量。
- 不复制用户数据库、任务数据库、日志、证书或 SSH 密钥。

## 传输与落盘

- 主站先生成仅包含白名单变量的临时文件，权限为 `600`。
- 使用加密 SSH/SCP 通道传输，不在聊天、命令输出或 Git 中显示值。
- 测试机保存为 `/etc/huangque-test/providers.env`，属主 `root:root`，权限 `600`。
- xray 节点配置保存为 `/etc/huangque-test/egress/xray-client.json`，只允许 root 与运行服务的受限用户读取；二进制安装为 `/usr/local/bin/xray-egress`。
- 传输完成后立即删除两端临时文件。
- systemd 服务通过 `EnvironmentFile=/etc/huangque-test/providers.env` 加载供应商配置。

## 服务变更

- 更新测试机内容与管理服务单元，使其同时加载测试基础配置和供应商配置。
- 新增 `huangque-test-egress.service`，先启动并验证 `127.0.0.1:10809`，内容类服务在其后启动。
- 根据现有路由启用需要独立进程的图片、采集和下载服务，并保持端口只监听回环地址。
- 更新 Nginx 精确路由，使独立能力进入对应后端，其余 `/api/gen/` 继续进入内容服务。
- 所有配置先经过语法检查，再按依赖顺序重启。

## 验证

1. 检查供应商文件存在、属主正确且权限为 `600`，但不输出内容。
2. 检查服务进程已加载所需变量名，仅报告“已设置/未设置”。
3. 检查 xray 出口服务状态、本地 10809 端口，以及通过代理的无凭据连通性；不输出节点配置。
4. 检查 Nginx 与 systemd 状态、回环端口和健康接口。
5. 使用最小、低费用请求逐项验证；任何会明显产生费用的生成请求需单独确认后执行。
6. 检查 Git 状态，确保密钥文件和运行时数据未被跟踪。

## 回滚

删除测试机 `/etc/huangque-test/providers.env`，恢复上一版 systemd/Nginx 配置并重启服务，即可回到基础测试环境。主站配置在整个过程中不修改，只创建和删除临时导出文件。
