# 抖音获客后台 — 蓝绿双实例架构（边用边修改不掉线）

## 拓扑
团队访问 http://<IP>:8090  →  nginx(轮询+健康检测+故障转移)
  ├─ 实例A  127.0.0.1:8091   systemd leadgen-A   代码目录 /home/ubuntu/leadgen-A
  └─ 实例B  127.0.0.1:8092   systemd leadgen-B   代码目录 /home/ubuntu/leadgen-B
共享态(软链到 /home/ubuntu/leadgen-server)：jobs.db(WAL) / files/ / discovered.json / keywords.json
> A、B 各有独立的 app.py + index.html（代码层隔离）；数据层共享，所以队列/结果一致。
> 关键：A/B 跑的是"独立副本"，不在 git 仓库目录里 → 别人 git pull/reset 冲不掉正在服务的版本。

## 日常更新（同事改完代码后）
1. 把新代码弄到服务器仓库：cd /home/ubuntu/douyin-leadgen && git pull
2. 一键滚动更新：/home/ubuntu/leadgen-deploy.sh roll
   - 先更新A→健康检查→等5s→再更新B；任一步健康检查不过自动回滚，团队全程不掉线。
3. 只更某一个：leadgen-deploy.sh A   或   leadgen-deploy.sh B
4. 看状态：leadgen-deploy.sh status

## 回滚
- 自动：部署后健康检查不过会自动回滚到 .bak。
- 手动应急：sudo systemctl start leadgen   （老的单实例 :8090 单元还在，先 systemctl stop nginx 再 start leadgen 即可秒回老架构）

## worker（爬虫层）不归 nginx 管
worker 是"从队列拉活"的模型(leadgen-worker@N)，天然负载均衡：多个 worker 抢同一个队列。
更新 worker 同理：一次重启一个(@1→@2→@3)，别的继续干活。号池一号一 worker 防风控。
