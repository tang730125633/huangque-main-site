#!/usr/bin/env bash
#
# 黄雀编导 Agent - director-breakdown 反推 真机 E2E 复跑脚本 (#1420)
# 用途: 在【已升级含 director 能力族的新版 hq_cli + 已 login 授权】的服务器上运行,
#       一次打出评审要的全部证据并落盘 JSON。跑完把本脚本输出区段粘回 PR #1420 即可。
#
# 用法:  bash e2e_director_breakdown.sh '<真实视频链接>'
# 注意: confirm 变体会真实扣点/提交 job。请先用小点数/测试账号跑,并确认你想要提交该制任务。
#       本脚本内 confirm 步骤默认只做【契约判别】不在无提示时自动提交真实付费任务。

set -u
VIDEO_URL="${1:-}"
if [ -z "$VIDEO_URL" ]; then
  echo "缺参数: bash $0 '<真实可解析视频链接>'"; exit 2
fi
OUT="director_breakdown_e2e_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUT"

echo "### E2E DIRECTOR-BREAKDOWN 复跑开始  URL=$VIDEO_URL  ###"
echo "日期: $(date -Is)"

echo "\n[0] 版本/能力 "
python3 -m hq_cli --version 2>&1 | tee "$OUT/00_version.txt"
python3 -m hq_cli capabilities --json 2>&1 | tee "$OUT/01_capabilities.json"
python3 -m hq_cli capabilities --json 2>/dev/null | grep -c director > "$OUT/01b_director_count.txt" || true
echo "director 能力数: $(cat "$OUT/01b_director_count.txt")  (0 = 服务器无 director 族, 需先升级 hq_cli)"

echo "\n[1] describe director-breakdown"
python3 -m hq_cli describe director-breakdown --json 2>&1 | tee "$OUT/10_describe.json"

echo "\n[2] quote (reverse_prompt url -> token/cost, confirm=false)"
echo "{\"mode\":\"reverse_prompt\",\"url\":\"$VIDEO_URL\"}" \
  | python3 -m hq_cli run director-breakdown --input @- --json > "$OUT/20_quote.json" 2>&1
rc=$?; echo "quote returncode=$rc"; cat "$OUT/20_quote.json"

# 提取关键字段作下一步变量(qt = quote_token 非空则可用)
QT=$(python3 - "$OUT/20_quote.json" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1]))
    r=(d.get("result") or {}) if isinstance(d,dict) else {}
    tok=r.get("quote_token") or d.get("quote_token") or ""
    print(tok)
except Exception:
    print("")
PY
)
COST=$(python3 - "$OUT/20_quote.json" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1])); r=(d.get("result") or {}) if isinstance(d,dict) else {}
    print(int(r.get("cost")) if isinstance(r.get("cost"),int) else "")
except Exception:
    print("")
PY
)
echo "quote_token=${QT:-<无>}  cost=${COST:-<无>}"

echo "\n[3] confirm 契约判别 --expected-cost 归属 (两种变体: 用一个无害 job 或仅观察错误, 请确保在能接受的账号/点数下做)"
if [ -n "$QT" ] && [ -n "$COST" ]; then
  echo "--- 变体1: 不带 --expected-cost ---"
  echo "{\"mode\":\"reverse_prompt\",\"url\":\"$VIDEO_URL\"}" \
    | python3 -m hq_cli run director-breakdown --input @- --quote-token "$QT" --confirm \
      --json > "$OUT/31_confirm_no_expected.json" 2>&1
  echo "rc=$?"; cat "$OUT/31_confirm_no_expected.json"
  echo "--- 变体2: 带 --expected-cost $COST ---"
  echo "{\"mode\":\"reverse_prompt\",\"url\":\"$VIDEO_URL\"}" \
    | python3 -m hq_cli run director-breakdown --input @- --quote-token "$QT" \
      --confirm --expected-cost "$COST" --json > "$OUT/32_confirm_with_expected.json" 2>&1
  echo "rc=$?"; cat "$OUT/32_confirm_with_expected.json"
else
  echo "quote 未返回可用 token/cost，confir 无法执行；请先修 quote 问题。"
fi

echo "\n[4] 结果目录已打包: $OUT/"
echo "把上面从 '[0]' 到 '[4]' 的输出(或整包 $OUT/) 粘贴/回传回 PR #1420 即可。"
