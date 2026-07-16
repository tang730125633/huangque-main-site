# 作图出境隧道（部署说明）

把作图三引擎（nb2 / pro / gpt）的官方 API 请求，从拥塞的 heygen 共享中转，改为优先走
自建 VPS Reality 隧道直连官方，前档超时/报错自动降级。

## 出境优先级链（`content_domains/egress.py`）

1. **首选** `EGRESS_PROXY` —— 新 VPS Reality 隧道的本地 http 代理（本机 xray-egress）
2. **备选** `EGRESS_PROXY_FALLBACK` —— 现有 mihomo（法兰克福），如 `http://127.0.0.1:7897`
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
curl -s -o /dev/null -m 20 -x http://127.0.0.1:10809 -w '%{http_code}\n' https://api.openai.com/v1/models  # 期望 200
```

## 二、打开代码里的出境链（content.env）

在 `/home/ubuntu/content-api/content.env` 增加：

```
EGRESS_PROXY=http://127.0.0.1:10809          # 首选：本机 VPS 隧道
EGRESS_PROXY_FALLBACK=http://127.0.0.1:7897  # 备选：现有 mihomo(法兰克福)
# EGRESS_TIMEOUT=210                          # 可选，每个代理档超时秒数（默认 210，覆盖 gpt-image-2 ~174s）
# EGRESS_PRIMARY_TIMEOUT=300                  # 可选，单独放宽首选(VPS)档超时（默认回落到 EGRESS_TIMEOUT）
```

> 超时须满足「首选 + 备选 + 兜底 < 900s」（reaper `image` 宽限），否则会边降级边被误判超时退点。
> 例：首选 300 + 备选 210(`EGRESS_TIMEOUT`) + 兜底 300(`EGRESS_HEYGEN_TIMEOUT` 默认) = 810s，安全。

> `GEMINI_BASE` / `OPENAI_BASE`（heygen）**保持不变**，它们是最后兜底档。

然后重启用到的服务（挑在飞任务少的窗口）：

```bash
sudo systemctl restart huangque-imggen-api   # nb2 / pro
sudo systemctl restart huangque-content      # gpt
```

## 回滚

删掉 content.env 里那两行 `EGRESS_*` 再重启两个服务，即刻退回全走 heygen 的老行为。
（隧道服务可留着不影响，代码不读 `EGRESS_*` 就不会用它。）

## 注意

- **官方 key 用黄雀现有的即可** —— 线上 `GEMINI_API_KEY` / `OPENAI_API_KEY` 实测都是官方有效
  key（heygen 原本也只是拿它们转发），直连官方无需换 key。
- 隧道长连接（如 gpt-image-2 ~174s）偶发被 RST 掐断属正常，egress 会自动降级到 mihomo/heygen，
  不影响出图，只是那一张会慢一点。
- 高并发瓶颈是**单个官方 key 的限速**（实测 5 并发每条涨到 ~50s），不是隧道；需要更高并发时
  应多配官方 key 轮询（与 issue 泽龙2 单 key 同类）。
