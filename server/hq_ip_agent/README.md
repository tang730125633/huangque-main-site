# 主 Agent v4：独立 hq-ip-agent 服务

这是线上 `/workbench/ip12/` 当前实际代理的 Flask / Waitress 服务，端口 8000、
工作目录 `/home/ubuntu/hq-ip-agent`、systemd 名称 `hq-ip-agent`。
它**不是**本仓库 `server/hermes_ip12`（Hermes IP12）或 `server/creator_agent`。
用户选择继续使用本服务，旧 Hermes 源码和部署入口保留，不覆盖、不停服、不清理。

## 来源和保留的回滚版本

- 本次首次纳入 GitHub 的业务源码来自生产本地 Git 提交
  `f5cc900c70a8752f3efeab7fcf7f34757558e984`；此前 origin 只是服务器本地 bare repo。
- GitHub 旧主线锚点：`83b6bb170da836d8039c5a1ff064b27b71de9398`。
- 未改动的 `server/hermes_ip12` Git tree：`328d52ebfc348a638162fec7e30c02378ab9ea4b`。
- 只纳入应用代码、业务 skill 源和离线测试；不含 `.env`、个人会话、数据库、缓存、
  生成结果、服务器密钥或第三方安装目录。不得把本目录整棵覆盖生产。
- 当前 PR 中多数新增行是源码归档，不代表本次线上改动。真实业务修复仅下述五个文件。

## 本次修复

1. `agent/v4/main_agent.py`：原最近 24 条历史的裁剪快照遗漏已保存画像；现在每轮带入
   画像，已回答、否定、跳过的信息不再作为缺项重新问。保留原始历史和会话文件。
2. `static/v4.js`：乱序结果队列的 8 秒兜底以前仍等待旧轮次，可能永久不显示已完成
   回复；现在超时强制显示已完成结果，同时保留旧轮次继续接收并按 seq 去重。
3. `app.py`、`agent/v4/subagent.py` 与 JS：确认卡携带业务域、报价身份、确认/取消
   决定；并发确认在原域锁内复核，只续接原报价，不重复创建；旧卡不能确认新报价。
4. `static/style.css`：手机报价正文与按钮分行，顶部两行弹性布局，按钮至少 44px。

前端唯一源码位于 `site/workbench/hq-ip-agent/`。本地 app 自动使用该目录；现有生产
仍使用 app 旁的 `static/`，不改变 URL、Nginx、服务配置或磁盘数据布局。

## 离线验收（不联网模型、不扣点）

```bash
python -m pip install -r server/hq_ip_agent/requirements.txt
python -m unittest discover -s tests -p test_hq_ip_agent_release.py -v
cd server/hq_ip_agent
PYTHONPATH=. python -m unittest discover -s tests -p test_ux_recovery.py -v
PYTHONPATH=. python tests/hq-p0c-test.py
npm install --no-save --package-lock=false playwright@1.56.1
npx playwright install chromium
node tests/ux_mobile_regression.mjs
```

GitHub `Main Agent v4 UX` 在 Linux 执行相同回归并保存三种手机宽度的截图与 JSON。
9 项问题测试、57 项相邻断言、9 项发布故障测试；360/390/412px 验证无溢出、按钮三连击
只发一次确认、永久旧轮次不阻塞新回复、已完成报价卡移除。
模型/CLI/业务 API 使用替身；不能把此测试宣称为真实扣点或真实微信内核验收。

## 从 GitHub main 精确部署与回滚

先通过 PR 和同 SHA CI，合并 main 后，在干净 checkout 中锁定最新 `origin/main`。
`deploy/hq-ip-agent-ux.json` 固定原五文件 SHA-256 和目标；
`scripts/hq_ip_agent_release.py --build <新目录> --commit <完整合并 SHA>`
只读取该提交的 Git blobs，生成五文件发布包、manifest 和版本化发布脚本。

通过保留主机校验的 SSH 上传到版本化 staging，核对 release.py 的本地/远端 SHA，
先执行 `python3 <stage>/release.py --bundle <stage> --commit <SHA>` 只读预检。
操作员确认无活跃轮次后，授权的生产账号加 `--apply` 执行；不提供密码参数。

发布仅替换 app.py、agent/v4/main_agent.py、agent/v4/subagent.py、static/v4.js、
static/style.css，备份位于 `/home/ubuntu/release-backup/hq-ip-agent-ux-*`（0700）。
原五文件/提交不匹配即停；停同一服务后再次核验，原子替换，启动 `hq-ip-agent`，
验证本机和公网健康、页面、JS/CSS 实际响应哈希。任何中途失败自动恢复五文件并启动
同一服务；若回滚仍失败，明确失败并保留备份位置。不会改 `.env`、数据或旧 Hermes。

旧 Hermes 的 Git 副本不是“已验证能无损切流”的承诺：启用它属于跨服务回退，必须另行
核对其真实运行版本、健康与 Nginx 路由后按范围确认，不能只改路由就声称完成回滚。
本次故障回滚始终优先恢复原 hq-ip-agent 五个文件，保持用户使用同一套服务和会话。
