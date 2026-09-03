# 黄雀·编导 Agent — director-breakdown(director) 反推闭环 真机复跑清单
对象：#1420 (feature/yuelei-local-agent-breakdown-closeloop)
前提：在**已升级含 director 能力族的新版 hq_cli（仓库 tools/hq-cli 已达 0.1x）+ 已 hq_cli login 授权**的服务器上执行。
这台提交机无真 hq_cli，无法代替以下端到端。请有权限者照此逐项复跑并把"证据/输出"回填，我再把契约与验收写进 PR 提审。

## 0) 前置确认
- [ ] CLI 版本含 director 能力（审者报告旧 0.13.0 无任何 director-*）
  `python3 -m hq_cli --version`
  `python3 -m hq_cli capabilities --json | grep -c director`   # 期望 >0
- [ ] 已授权：`python3 -m hq_cli login --json` 成功
- [ ] 环境含本 PR 分支代码可运行（agent-prototypes/yuelei-local-agent）

## 1) describe director-breakdown（能力确实存在 + JSON schema）
```bash
python3 -m hq_cli describe director-breakdown --json
```
期望：返回 {schema:"hq.describe/v1"...}, action/input 结构里含 mode/reverse_prompt、url、urls 说明，以及 quote/confirm 契约字段(quote_token / cost / confirm 方式/ --expected-cost 归属)。

## 2) quote：URL 反推请求是否先生成 token & cost（confirm=false）
用一段真实链接（抖音/视频号/小红书均可解析的）：
```bash
echo '{"mode":"reverse_prompt","url":"<真实可解析视频链接>"}' \
 | python3 -m hq_cli run director-breakdown --input @- --json
```
记录：`result.quote_token`（非空）、`result.cost`（int）、`expires_in`；若报 error，把 error/detail 原样保存。

## 3) confirm：quote_token 到底要不要 `--expected-cost`（关键争议点）
对比两种调用（各只跑会在无效时就拒绝，不实际扣成功都不产生费用；若会扣则先在小点数/测试账号做）：
```bash
# 变体1：不带 --expected-cost
echo '{"mode":"reverse_prompt","url":"<链接>"}' \
 | python3 -m hq_cli run director-breakdown --input @- --quote-token <TOKEN> --confirm --json
# 变体2：带 --expected-cost <cost>
echo '{"mode":"reverse_prompt","url":"<链接>"}' \
 | python3 -m hq_cli run director-breakdown --input @- --quote-token <TOKEN> --confirm --expected-cost <COST> --json
```
记录各自 returncode/payload：哪个成功拿 job_id；失败的错误码(error/detail/code)原样保存。这决定--expected-cost 归属 director-breakdown 还是只属 director-breakdown-upload(#1408 注释)。

## 4) 端到端回贴链路（对应 #1420 UI）
- 起 local_ui.py(给真实站点目录) → 浏览器 agent-lab.html
- 顾客消息粘真实链接→出现"报价卡(扣N点)"；点确认→ 观察是否 job 提交、轮询到结果、结构/镜头/口播/提示词气泡回贴
- 重复发同一链接/“再来一次”→ 确认只展示已有报价卡(去重)，不重复扣
- 记返回: job_id, cost 实扣, result 结构回贴样例（脱敏后贴出）

## 5) 证据交付（回给我/回 PR）
把以上每步命令输出、returncode、有无 error、以及“变体1 vs2”结果，原样贴回（可脱敏链接/账号）。我据实把 quote/confirm 契约与是否 `--expected-cost` 写进 #1420 说明 + 必要时代码对齐，再提请合。
