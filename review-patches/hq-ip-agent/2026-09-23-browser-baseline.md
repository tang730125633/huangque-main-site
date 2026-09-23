# hq-ip-agent 浏览器门禁基线修复审核单

## 背景与目标

独立 `hq-ip-agent` 仓库在引入登录态启动流程后，部分浏览器套件没有为自动触发的只读启动请求准备测试夹具，导致 `tests/run_all.sh` 无法作为 PR 1668 的可靠硬门禁。本补丁只修复测试基础设施，不修改任何生产代码、数据库、计费、Provider 调用或用户状态。

## 修复内容

- 共享浏览器夹具按显式环境开关启用，仅精确代答指定只读 GET 请求：`/api/auth/me`、`/api/v4/assets`、`/api/v4/sessions`、`/api/v4/tasks/*` 和 `/api/report/*`。
- `tests/run_all.sh` 会检查各套件是否已有对应路由；已有时关闭共享代答，保留套件自己的业务场景与断言。
- XSS 套件显式保留恶意报告链接载荷，确保共享夹具不会把安全负向用例替换为空数据。
- 布局套件用状态化报告夹具模拟“轮次完成后出现、状态轮询保持、重置后清除”的真实生命周期，并保留桌面、窄屏与重置断言。
- 门禁仍对任意断言失败或进程非零退出整体失败；未增加 skip、错误吞噬或阈值放宽。

## 固定审核对象

- 上游基线：`hq-ip-agent@52d85739b01f7079f59a88a12329bd04c5a91ff2`
- 修复 HEAD：`4ac7d60c0c17fc0ee5a098c81d9ca9f0f6e522b2`
- 预期结果树：`f834de93a057dfa03b196ddd43af635919e0fea1`
- 补丁 SHA-256：`1e833b7d74ce693f856b5c22e051f563c3037a74853a7cd5db80e254bce34aaf`

涉及文件：

- `tests/browser-auth-fixture.mjs`
- `tests/run_all.sh`
- `tests/hq-p0-reset-xss-test.mjs`
- `tests/hq-layout-test.mjs`

## 验证结果

- 完整浏览器回归：`328 PASS / 0 FAIL`。
- 完整单元与集成门禁：`tests/run_unit.sh` 通过。
- 定向验证：布局、XSS/reset、附件上传套件通过。
- `node --check`、`git diff --check`、敏感信息模式扫描通过。
- Standards 复审：P0/P1/P2 = `0/0/0`，通过。
- Spec 复审：P0/P1/P2 = `0/0/0`，通过。
- 补丁已在精确基线执行 `git apply --check --unidiff-zero --index` 和 `git apply --unidiff-zero --index`；`git write-tree` 与预期结果树完全一致。

## 审核通过后的应用方式

在干净、固定到上述完整上游基线的 `hq-ip-agent` 工作区执行：

```bash
sha256sum review-patches/hq-ip-agent/2026-09-23-browser-baseline.patch
git apply --check --unidiff-zero --index review-patches/hq-ip-agent/2026-09-23-browser-baseline.patch
git apply --unidiff-zero --index review-patches/hq-ip-agent/2026-09-23-browser-baseline.patch
test "$(git write-tree)" = "f834de93a057dfa03b196ddd43af635919e0fea1"
```

应用后必须重新运行完整浏览器与单元门禁，再在独立仓库按其受保护分支流程合入。本审核单只记录可重放补丁，不部署、不修改生产服务，也不直接推送独立仓库 `main`。
