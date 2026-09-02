# 作图与采集出境隧道（部署说明）

把作图三引擎（nb2 / pro / gpt）的官方 API 请求，从拥塞的 heygen 共享中转，改为优先走
自建 VPS Reality 隧道直连官方，前档超时/报错自动降级。

## 出境优先级链（`content_domains/egress.py`）

1. **首选** `EGRESS_PROXY` —— 本机 `xray-hqvps` HTTP 代理（生产当前为 `127.0.0.1:10811`）
2. **备选** `EGRESS_PROXY_FALLBACK` —— 独立的 `xray-egress-novix` HTTP 代理（生产当前为 `127.0.0.1:10810`）
3. **兜底** heygen 中转 —— `GEMINI_BASE` / `OPENAI_BASE`，直连

> 两个 `EGRESS_*` 都不配时，链里只剩 heygen 一档 = 改动前的老行为。**代码合并零风险；
> 真正切换靠下面的部署。**

## 一、装隧道客户端（xray）

```bash
# 1. 放二进制（与 imggen 侧临时探测用的同一个 xray 即可）
sudo install -m755 xray /usr/local/bin/xray-egress

# 2. 放真实配置（含 VPS 节点凭据，600，不进 git）
mkdir -p /home/ubuntu/egress
cp xray-client.example.json /home/ubuntu/egress/xray-client.json
chmod 600 /home/ubuntu/egress/xray-client.json
#   按 3X-UI 面板导出的 vless:// 链接，把 <占位符> 换成真实 UUID/公钥/SNI/ShortID/端口/flow

# 3. 装服务
sudo cp deploy/systemd/huangque-egress-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now huangque-egress-tunnel

# 4. 自检：本地代理口通、且穿隧道摸得到官方
ss -tlnp | grep 10809
curl -s -o /dev/null -m 20 -x http://127.0.0.1:10809 -w '%{http_code}\n' https://api.openai.com/v1/models  # 不带 Key 时期望 401
```

生产环境当前另有两套独立客户端：

- `xray-hqvps.service` → `127.0.0.1:10811`（主）；
- `huangque-egress-novix-client.service` → `127.0.0.1:10810`（备）。

`127.0.0.1:7999` 的 Mihomo 线路曾在 2026-09-01 对图片、视频、COS、OpenAI、Gemini
全部返回 TLS `unexpected eof`。在重新完成多轮探针前，不得把它设为主出口或 fallback；
`service active` 只证明进程存在，不证明隧道可用。

## 二、打开代码里的出境链（content.env）

在 `/home/ubuntu/content-api/content.env` 增加：

```
HTTP_PROXY=http://127.0.0.1:10810
HTTPS_PROXY=http://127.0.0.1:10810
ALL_PROXY=http://127.0.0.1:10810
EGRESS_PROXY=http://127.0.0.1:10811          # 首选：xray-hqvps
EGRESS_PROXY_FALLBACK=http://127.0.0.1:10810 # 备选：Novix Reality
NO_PROXY=localhost,127.0.0.1,zelong.vip,huangquechuanmei.com,huangque-media-1435693839.cos.ap-guangzhou.myqcloud.com
no_proxy=localhost,127.0.0.1,zelong.vip,huangquechuanmei.com,huangque-media-1435693839.cos.ap-guangzhou.myqcloud.com
# EGRESS_TIMEOUT=210                          # 可选，每个代理档超时秒数（默认 210，覆盖 gpt-image-2 ~174s）
# EGRESS_PRIMARY_TIMEOUT=300                  # 可选，单独放宽首选(VPS)档超时（默认回落到 EGRESS_TIMEOUT）
```

`NO_PROXY/no_proxy` 中的 COS 精确域名不可省略：采集服务的腾讯 COS 图片/视频转存应走国内直连，
不能继承境外 `HTTP_PROXY`。主出口与 fallback 也必须是不同的已验证线路。

> 超时须满足「首选 + 备选 + 兜底 < 900s」（reaper `image` 宽限），否则会边降级边被误判超时退点。
> 例：首选 300 + 备选 210(`EGRESS_TIMEOUT`) + 兜底 300(`EGRESS_HEYGEN_TIMEOUT` 默认) = 810s，安全。

> `GEMINI_BASE` / `OPENAI_BASE`（heygen）**保持不变**，它们是最后兜底档。

然后重启用到的服务（挑在飞任务少的窗口）：

```bash
sudo systemctl restart huangque-imggen-api   # nb2 / pro
sudo systemctl restart huangque-content      # gpt
sudo systemctl restart huangque-leadgen-api  # 图片/视频采集、ASR、COS 转存
```

重启前先确认对应 kind 没有 `pending/running/processing/queued` 任务；重启后必须回读进程的
`/proc/<pid>/environ`，不能只看 env 文件内容。env 文件已更新但服务未重启时，老进程仍会继续使用旧代理。

## 生产验收

至少覆盖：

```bash
# 主、备线路都要跑；以下 401/403 表示 TLS/路由可达，不表示已认证。
curl -m 10 -x http://127.0.0.1:10811 -o /dev/null -w '%{http_code}\n' https://api.openai.com/v1/models
curl -m 10 -x http://127.0.0.1:10810 -o /dev/null -w '%{http_code}\n' https://generativelanguage.googleapis.com/v1beta/models

# 图片与视频片段必须返回 200/206 和非零字节。
curl -m 10 -x http://127.0.0.1:10811 -o /dev/null https://www.gstatic.com/webp/gallery/1.jpg
curl -m 10 -x http://127.0.0.1:10811 -H 'Range: bytes=0-262143' -o /dev/null https://media.w3.org/2010/05/sintel/trailer.mp4

# COS 走 NO_PROXY；未带签名访问 Bucket 根返回 403 即证明 DNS/TLS 可达。
curl -m 10 -o /dev/null -w '%{http_code}\n' https://huangque-media-1435693839.cos.ap-guangzhou.myqcloud.com/
```

不能只验一次：图片、视频片段至少各 3 轮，随后检查 Leadgen/Imggen 重启后的 journal 中没有
`SSLEOFError`、timeout 或 traceback。COS 再用现有 SDK 做一次只读 `HEAD Bucket`，不要为了验收写测试对象。

## 回滚

优先恢复改动前的 `content.env` 备份并重启受影响服务。紧急降级时可把 `EGRESS_PROXY` 与
`EGRESS_PROXY_FALLBACK` 都移除，退回 heygen 兜底；不要回切未经探针验证的 7999。
（隧道服务可留着不影响，代码不读 `EGRESS_*` 就不会用它。）

## 注意

- **官方 key 用黄雀现有的即可** —— 线上 `GEMINI_API_KEY` / `OPENAI_API_KEY` 实测都是官方有效
  key（heygen 原本也只是拿它们转发），直连官方无需换 key。
- 隧道长连接（如 gpt-image-2 ~174s）偶发被 RST 掐断属正常，egress 会自动降级到 mihomo/heygen，
  不影响出图，只是那一张会慢一点。
- 高并发瓶颈是**单个官方 key 的限速**（实测 5 并发每条涨到 ~50s），不是隧道；需要更高并发时
  应多配官方 key 轮询（与 issue 泽龙2 单 key 同类）。
