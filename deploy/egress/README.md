# 作图出境隧道（部署说明）

把作图三引擎（nb2 / pro / gpt）的官方 API 请求，从拥塞的 heygen 共享中转，改为优先走
自建 VPS Reality 隧道直连官方，前档超时/报错自动降级。

## 出境优先级链（`content_domains/egress.py`）

1. **首选** `EGRESS_PROXY` —— 本机 xray-egress Reality 隧道（10809，稳定）
2. **备选** `EGRESS_PROXY_FALLBACK` —— mihomo-new SS 节点（7999，间歇性波动）
3. **兜底** heygen 中转 —— `GEMINI_BASE` / `OPENAI_BASE`，直连

> **⚠️ 2026-07-21 线上故障复盘**：原配置以 mihomo(7999) 为首选，xray(10809) 为备选。
> 但 mihomo-new 的 SS 节点 `transferone.agrayfox.top` 间歇性故障（TLS EOF、connection refused、
> 中途 RST），导致 7/14~7/20 期间生图失败率高达 34%（99/289 任务失败）。
> **现统一改为 xray(10809) 为首选**，该通道从 7/19 至今零报错。

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

在 `/home/ubuntu/content-api/content.env` 配置：

```bash
# 作图出境优先级链: VPS隧道(10809) → mihomo(7999) → heygen兜底
# ⚠️ 建议 xray(10809) 为首选（稳定），mihomo(7999) 为备选（SS节点间歇波动）
EGRESS_PROXY=http://127.0.0.1:10809          # 首选：本机 VPS Reality 隧道
EGRESS_PROXY_FALLBACK=http://127.0.0.1:7999  # 备选：mihomo-new SS 节点
EGRESS_PRIMARY_TIMEOUT=300                   # 首选超时（300s覆盖gpt-image-2 ~174s）
# EGRESS_TIMEOUT=210                          # 可选，备选档超时秒数（默认 210）
# EGRESS_HEYGEN_TIMEOUT=300                   # 可选，兜底档超时（默认 300）

# 进程级代理：也走稳定隧道
HTTP_PROXY=http://127.0.0.1:10809
HTTPS_PROXY=http://127.0.0.1:10809
ALL_PROXY=socks5://127.0.0.1:7999            # xray 只支持 HTTP，SOCKS5 走 mihomo
http_proxy=http://127.0.0.1:10809            # 小写兜底（部分 Python 库只读小写）
https_proxy=http://127.0.0.1:10809
```

> 超时须满足「首选 + 备选 + 兜底 < 900s」（reaper `image` 宽限），否则会边降级边被误判超时退点。
> 例：首选 300 + 备选 210 + 兜底 300 = 810s，安全。

> `GEMINI_BASE` / `OPENAI_BASE`（heygen）**保持不变**，它们是最后兜底档。

然后重启用到的服务：

```bash
sudo systemctl restart huangque-imggen-api   # nb2 / pro
sudo systemctl restart huangque-content      # gpt
```

## 回滚

```bash
# 恢复备份
cp /home/ubuntu/content-api/content.env.bak.egress-fix-* /home/ubuntu/content-api/content.env
sudo systemctl restart huangque-content huangque-imggen-api
```

## 监控

部署后关注以下指标：
- `journalctl -u huangque-content | grep '\[egress\].*via vps.*失败'` → 应为 0
- `journalctl -u huangque-content | grep 'IncompleteRead\|Remote end closed'` → 应大幅减少
- 生图成功率 > 85%（当前基线 65.7%）

## 注意

- **官方 key 用黄雀现有的即可** —— 线上 `GEMINI_API_KEY` / `OPENAI_API_KEY` 实测都是官方有效
  key（heygen 原本也只是拿它们转发），直连官方无需换 key。
- mihomo-new 单 SS 节点仍存在单点风险，后续应加固（多节点 url-test 自动选优）。
- 高并发瓶颈是**单个官方 key 的限速**（实测 5 并发每条涨到 ~50s），不是隧道；需要更高并发时
  应多配官方 key 轮询。
