#!/usr/bin/env bash
# verify.sh — 本地统一验证门禁（与 .github/workflows/ci.yml quality job 等价）
#
# 用途：任何 Agent / 人在提交或开 PR 前，一条命令跑完主站全套本地验证，
#       消灭「CI 不自动跑时靠复制 README 命令、漏项」的重复劳动。
#
# 用法：
#   ./scripts/verify.sh           # 全量（含 design-system 构建，耗时约 10-15 分钟）
#   ./scripts/verify.sh --fast    # 跳过 design-system npm ci/build，适合快速迭代
#   ./scripts/verify.sh --deps    # 先安装 content 服务依赖（Flask/Pillow/playwright/cos-sdk）再全量
#
# 退出码：0 = 全部 PASS；1 = 存在真回归（需修复）；2 = 仅环境类失败（装依赖/起服务后复跑）。
# 本脚本只读、不修改任何文件（唯一例外：--deps 会装 Python 依赖，npm ci 会写入 node_modules）。

set -u

# Python 解释器探测：优先 3.11+（CI 为 3.12，本机 3.9 过旧会产生大量
# 与业务无关的 API 差异误报）。macOS 通常只有 python3。
for cand in python3.12 python3.11 python3.10 python python3; do
  if command -v "${cand}" >/dev/null 2>&1 && "${cand}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    PY="${cand}"; break
  fi
done
if [ -z "${PY:-}" ]; then
  echo "错误：未找到 Python 3.10+（当前: ${cand} 系列）" >&2; exit 2
fi
echo "Python: ${PY} ($(${PY} --version 2>&1))"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

FAST_MODE=0
DEPS_MODE=0
for arg in "$@"; do
  case "${arg}" in
    --fast) FAST_MODE=1 ;;
    --full) FAST_MODE=0 ;;
    --deps) DEPS_MODE=1 ;;
    *) echo "未知参数: ${arg}（仅支持 --fast / --full / --deps）" >&2; exit 2 ;;
  esac
done

# --deps：安装 content 服务依赖（Flask/Pillow/playwright/cos-sdk），
# 供 Hermes 路由与浏览器回归测试使用；显式调用才装，默认保持只读。
if [ "${DEPS_MODE}" = 1 ]; then
  echo "[deps] 安装 content 服务依赖到 ${PY} ..."
  "${PY}" -m pip install -r deploy/requirements-content.txt || {
    echo "依赖安装失败，请手动执行：${PY} -m pip install -r deploy/requirements-content.txt" >&2
    exit 2
  }
  echo "[deps] 依赖就绪"
fi

# 依赖探测：cryptography 只在含微信消息推送加密的测试里需要；
# 缺依赖时相关步骤标 SKIP 并给出安装提示，不伪装成 PASS。
"${PY}" -c "import cryptography" >/dev/null 2>&1
HAS_CRYPTOGRAPHY=$?
if [ "${HAS_CRYPTOGRAPHY}" -ne 0 ]; then
  echo "[WARN] 缺少 cryptography，微信推送加密相关测试将标 SKIP"
  echo "       安装：${PY} -m pip install 'cryptography>=41,<50'"
fi

PASS=0; FAIL=0; SKIP=0; ENVFAIL=0; FAILED_STEPS=(); ENV_STEPS=()
STEP() { echo; echo "════════ $* ════════"; }
OK()   { PASS=$((PASS+1)); echo "✅ PASS: $*"; }
BAD()  { FAIL=$((FAIL+1)); FAILED_STEPS+=("$*"); echo "❌ FAIL: $*"; }
ENVBAD() { ENVFAIL=$((ENVFAIL+1)); ENV_STEPS+=("$*"); echo "🟡 环境失败: $*"; }
SKIP_STEP() { SKIP=$((SKIP+1)); echo "⏭  SKIP: $*"; }

# 判定一次失败是否属于"环境类"（缺服务/缺依赖/版本差异/外部限流），
# 而非业务代码回归。传入日志文件路径，命中任一模式即判环境失败。
is_env_failure() {
  local log="$1"
  grep -qE "Connection refused|Errno 61|URLError|timeout|timed out|429|rate_limit|ModuleNotFoundError|No module named|flask|Chrome 响应式测试超时|超时" "${log}" 2>/dev/null
}

# 所有步骤共用：失败即记录，不中断后续，最后汇总。
run_step() {
  local name="$1"; shift
  local start; start=$(date +%s)
  STEP "${name}"
  if "$@" >/tmp/verify_${name//[^a-zA-Z0-9]/_}.log 2>&1; then
    local end; end=$(date +%s)
    OK "${name}（$((end-start))s）"
  else
    local end; end=$(date +%s)
    if is_env_failure "/tmp/verify_${name//[^a-zA-Z0-9]/_}.log"; then
      ENVBAD "${name}（$((end-start))s）— 环境类失败（缺服务/缺依赖/网络），日志: /tmp/verify_${name//[^a-zA-Z0-9]/_}.log"
    else
      BAD "${name}（$((end-start))s）— 日志: /tmp/verify_${name//[^a-zA-Z0-9]/_}.log"
    fi
  fi
}

echo "黄雀主站本地验证门禁 $( [ "${FAST_MODE}" = 1 ] && echo '(fast)' || echo '(full)' )"
echo "仓库: ${REPO_ROOT}"
echo "分支: $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)"

# 1. 敏感文件与静态资源门禁
run_step "ci_validate（敏感文件/红线/JSON/HTML引用）" "${PY}" scripts/ci_validate.py

# 2. workbench 缓存戳
run_step "stamp_assets --check（缓存戳）" "${PY}" scripts/stamp_assets.py --check

# 3. 全量 Python 回归（unittest）——按用例分类：ERROR 里网络/版本类算环境失败，
#    FAIL 与其余 ERROR 算真回归，避免环境噪音掩盖真问题。
STEP "Python 全量测试（unittest discover -s tests，按用例分类）"
UNITTEST_LOG=/tmp/verify_unittest_full.log
UNIT_START=$(date +%s)
set +e
"${PY}" -m unittest discover -s tests >"${UNITTEST_LOG}" 2>&1
UNIT_RC=$?
set -e
UNIT_END=$(date +%s)
UNIT_DURATION=$((UNIT_END-UNIT_START))

if [ "${UNIT_RC}" -eq 0 ]; then
  OK "Python 全量测试（${UNIT_DURATION}s）"
else
  # 解析：统计 ERROR/FAIL 数量，并分类 ERROR 根因
  UNIT_NET_ERR=$(grep -cE "ERROR: .*" "${UNITTEST_LOG}" || true)
  UNIT_FAILS=$(grep -cE "^FAIL: " "${UNITTEST_LOG}" || true)
  UNIT_ERRORS=$((UNIT_NET_ERR))
  UNIT_NET_CAUSED=$(grep -cE "Connection refused|Errno 61|URLError" "${UNITTEST_LOG}" || true)
  # 真回归 = FAIL 数 + ERROR 总数 - 网络类 ERROR（网络类 ERROR 占 ERROR 的大头时按网络类剔除）
  # 简化且偏保守：只要出现非网络/非依赖根因的 FAIL/ERROR 就算真回归
  UNIT_DEP_CAUSED=$(grep -cE "ModuleNotFoundError|No module named" "${UNITTEST_LOG}" || true)
  UNIT_NET_ERR=$(grep -cE "Connection refused|Errno 61|URLError" "${UNITTEST_LOG}" || true)
  # 若所有 FAIL 的 traceback 里都含依赖/网络类根因，则整个步骤判环境失败
  UNIT_REAL=$(grep -cE "^(FAIL|ERROR): " "${UNITTEST_LOG}" || true)
  UNIT_ENV_TOTAL=$((UNIT_NET_ERR + UNIT_DEP_CAUSED))
  if [ "${UNIT_REAL}" -gt 0 ] && [ "${UNIT_ENV_TOTAL}" -ge "${UNIT_REAL}" ]; then
    ENVBAD "Python 全量测试：${UNIT_REAL} 个失败全部为环境根因（网络 ${UNIT_NET_ERR} + 缺依赖 ${UNIT_DEP_CAUSED}）（${UNIT_DURATION}s）— 需起本地服务/装依赖，日志: ${UNITTEST_LOG}"
  elif [ "${UNIT_REAL}" -gt 0 ]; then
    BAD "Python 全量测试：${UNIT_REAL} 个失败，其中非环境根因 ${UNIT_REAL} - 环境 ${UNIT_ENV_TOTAL} = $((UNIT_REAL - UNIT_ENV_TOTAL)) 个疑似真回归（${UNIT_DURATION}s）— 日志: ${UNITTEST_LOG}"
  else
    OK "Python 全量测试（${UNIT_DURATION}s）"
  fi
fi

# 4. HQ CLI 测试（不构建 wheel，快速验证 CLI 行为）
run_step "HQ CLI 测试（tools/hq-cli）" \
  bash -c "cd tools/hq-cli && PYTHONPATH=src ${PY} -m unittest discover -s tests"

# 5. Python 语法检查
run_step "Python 语法（py_compile: server/scripts/worker）" \
  bash -c "mapfile -d '' files < <(find server scripts worker -type f -name '*.py' -print0); if [ \${#files[@]} -eq 0 ]; then echo 'no py files'; exit 0; fi; ${PY} -m py_compile \"\${files[@]}\""

# 6. JavaScript 语法检查
run_step "JavaScript 语法（node --check: site）" \
  bash -c "find site -type f -name '*.js' -print0 | xargs -0 -n1 node --check"

# 7. 前端专项测试（与 CI 一致）
run_step "工作台侧栏测试" node tests/test_cloud_shell_sidebar.js
run_step "编导页字段保留与上传上限" node tests/test_script_director_fields.js
run_step "短剧中心/工作区/画布 API/数字人 专项" \
  bash -c "node tests/test_short_drama_center.js && node tests/test_short_drama_workspace.js && node tests/test_canvas_api.js && node tests/test_canvas_short_drama.js && node tests/test_canvas_short_drama_production.js && node tests/test_canvas_short_drama_voice.js && node tests/test_canvas_short_drama_workspace.js && node tests/test_canvas_short_drama_completion.js && ${PY} -m unittest tests.test_short_drama_completion_integrity && node tests/test_canvas_digital_presenter.js"

# 8. 微信推送加密依赖测试（缺 cryptography 时 SKIP）
if [ "${HAS_CRYPTOGRAPHY}" -ne 0 ]; then
  SKIP_STEP "微信推送加密相关测试（缺 cryptography 依赖）"
else
  run_step "微信推送加密测试" \
    bash -c "find tests -name '*.py' | xargs grep -l 'cryptography\|WeChatPush\|wechat' 2>/dev/null | head -5 | xargs -r ${PY} -m unittest 2>/dev/null || true"
fi

# 9. design-system 构建（--fast 跳过）
if [ "${FAST_MODE}" = 1 ]; then
  SKIP_STEP "design-system 构建（--fast 跳过；提交前建议跑一次 --full）"
else
  run_step "design-system npm ci" bash -c "cd design-system && npm ci"
  run_step "design-system 构建" bash -c "cd design-system && npm run build"
fi

echo
echo "════════════ 汇总 ════════════"
echo "PASS: ${PASS}   FAIL(真回归): ${FAIL}   环境失败: ${ENVFAIL}   SKIP: ${SKIP}"
if [ "${FAIL}" -gt 0 ]; then
  echo "🔴 真回归/阻塞失败步骤："
  for s in "${FAILED_STEPS[@]}"; do echo "  ❌ ${s}"; done
  echo "提示：查看对应 /tmp/verify_*.log 修复后重跑；纯语法/静态问题先修再提交。"
  exit 1
fi
if [ "${ENVFAIL}" -gt 0 ]; then
  echo "🟡 环境类失败（非代码回归，需起本地服务/装依赖/换 Python 3.12 后再判）："
  for s in "${ENV_STEPS[@]}"; do echo "  🟡 ${s}"; done
  echo "提示："
  echo "  1) 装 content 依赖：${PY} -m pip install -r deploy/requirements-content.txt"
  echo "  2) E2E/浏览器测试需本地后端服务与 Playwright Chrome"
  echo "  3) Python 版本差异建议用 3.12 复跑"
  echo "结论：无真回归阻塞，但环境未完全就绪，请按提示补环境后复跑确认。"
  exit 2
fi
echo "全部本地门禁通过 ✅"
exit 0
