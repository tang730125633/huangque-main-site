#!/usr/bin/env bash
# 一键本地回归：跑全部 10 个 Playwright 套件，汇总 PASS/FAIL。
# 前置：本地服务已起（cd 仓库根 && .venv/bin/python app.py，默认 http://127.0.0.1:8000）
# 环境变量：HQ_BASE 覆盖目标地址；HQ_PW_MODULE / HQ_CHROMIUM 覆盖 Playwright 安装路径
set -u
cd "$(dirname "$0")"
NODE_BIN="${HQ_NODE:-node}"

SUITES=(hq-intent-test hq-restore-intent-test hq-ui-test hq-layout-test hq-media-test hq-attach-test \
        hq-audio-slots-test hq-first-entry-test hq-p0-reset-xss-test hq-p0-drain-test hq-p0b-test hq-p0a-test hq-scroll-test)

total_pass=0; total_fail=0; failed_suites=()
for t in "${SUITES[@]}"; do
  echo "===== $t ====="
  if out=$("$NODE_BIN" "$t.mjs" 2>&1); then
    rc=0
  else
    rc=$?
  fi
  echo "$out" | grep -E '"PASS"|"FAIL"|ERROR' | tail -30
  p=$(echo "$out" | grep -c '"PASS"'); f=$(echo "$out" | grep -c '"FAIL"')
  total_pass=$((total_pass + p)); total_fail=$((total_fail + f))
  if [ "$f" -gt 0 ] || [ "$rc" -ne 0 ]; then
    failed_suites+=("$t")
  fi
  echo
done

echo "======================================"
echo "合计：$total_pass PASS / $total_fail FAIL"
if [ "${#failed_suites[@]}" -gt 0 ]; then
  echo "失败套件：${failed_suites[*]}"
  exit 1
fi
echo "全绿 ✅"
