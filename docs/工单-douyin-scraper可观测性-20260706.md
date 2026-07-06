# 工单 · douyin-scraper 高 CPU 与可观测性（#241）

> 冲突组：F/运维。领取人：Codex。禁止直接在服务器修改代码；先只读取证，再把可复现配置纳入 Git。

## 问题

`/home/ubuntu/douyin-scraper/.venv/bin/python start.py` 自 2026-06-24 裸跑，长期约 42% CPU，无 systemd unit、近期日志或退出告警，无法判断有效工作、卡死或无限重试。

## 实施

1. 只读取证进程父子关系、启动命令、工作目录、打开文件/网络连接、线程 CPU、日志配置和实际产出。
2. 根据证据判断根因；不先假设必须重启或新建服务。
3. 若仍需常驻：新增受 Git 管理的 systemd unit，输出进 journal，限制重启风暴与资源占用；代码补最小结构化日志。
4. 把该进程纳入现有健康监控；异常持续时告警。

## 验收

- 能说明 CPU 消耗来自哪个线程/循环及是否有有效产出。
- 进程由 systemd 管理，启动、停止、重启和日志均可追踪。
- 日志不包含 cookie、密钥、用户数据；有合理轮转。
- CPU/内存或连续失败超过阈值会告警；现有 leadgen 服务不受影响。

## 只读取证结论（2026-07-06）

- 进程实际属于 `xiaotan.service`，PID 1016 位于 `/system.slice/xiaotan.service`，不是裸进程；`StandardOutput=journal`，7 月 5 日仍有请求日志。
- PID 1016 是 Uvicorn reload 父进程，长期 42.4% CPU；真正监听 8501 的子进程 PID 1995 仅约 0.1% CPU，接口 `/docs` 返回 200。
- 上游 `start.py` 强制 `reload=True`，服务器未安装 `watchfiles`，Uvicorn 使用 StatReload。工作目录 6,609 个文件中 `.venv` 占 6,443 个，父进程持续轮询导致空转。
- 修复采用 Git 管理的 systemd unit 直接运行 `python -m uvicorn app.main:app`，生产关闭 reload；不修改或 vendor 上游目录。

## 部署

仅从已合并的 `main` 部署本工单新增文件；若需终止现有裸进程或切换服务，必须在部署步骤明确说明并验证业务无中断。
