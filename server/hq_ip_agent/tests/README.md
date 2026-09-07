# tests/ 回归测试说明

## 结构

| 文件 | 覆盖内容 |
|------|---------|
| `hq-intent-test.mjs` | 意图门控：出片卡/配置卡随意图挂载卸载 |
| `hq-restore-intent-test.mjs` | 恢复历史后的意图门控状态 |
| `hq-ui-test.mjs` | 出片配置卡交互全流程（22 断言） |
| `hq-layout-test.mjs` | 报告栏布局/粘顶/窄屏单列 |
| `hq-media-test.mjs` | 采集贴图：恢复渲染、图片真实加载、SSE 实时贴图 |
| `hq-p0-reset-xss-test.mjs` | reset 关旧 SSE 防串流 + 报告链接 XSS 转义 |
| `hq-p0-drain-test.mjs` | 刷新窗口排空去重、孤儿轮次兜回、及时收工 |
| `hq-p0b-test.mjs` | 出错轮不清界面、恢复失败重试、卡死任务超时 |
| `hq-p0a-test.mjs` | 双通道乱序排序、SSE 断线固定降级 HTTPS 轮询、贴图断线补偿、800 条批量渲染 |
| `hq-scroll-test.mjs` | 滚动锚点：手机端发消息不滚进轨迹面板、翻看轨迹时不被打断 |
| `hq-prod-p0-check.mjs` | 线上冒烟：真实会话恢复、去重、图片加载、零页面错误 |
| `hq-prod-media-check.mjs` | 线上媒体路由冒烟 |
| `hq-p0c-test.py` | B 批后端单测：persist 并发、livecaps 锁外、status 缓存、注册表回收、异步 start/同源校验/CLI 并发闸、日志 sid/seq 字段、skill 同步三态与 sync_skills 脚本（51 断言） |
| `run_all.sh` | 一键本地回归（10 套件 + 汇总） |
| `run_unit.sh` | 一键后端单测 |

## 跑法

```bash
# 1. 起本地服务（仓库根目录）
.venv/bin/python app.py

# 2. 另开终端跑回归
bash tests/run_all.sh        # 本地全量回归
bash tests/run_unit.sh       # 后端单测

# 3. 线上冒烟（对生产环境，注意别用真实用户会话之外的数据）
HQ_BASE=https://huangquechuanmei.com/workbench/ip12/ node tests/hq-prod-p0-check.mjs
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HQ_BASE` | `http://127.0.0.1:8000`（本地套件）/ 生产地址（prod 套件） | 目标站点根地址 |
| `HQ_PW_MODULE` | 本机 playwright 安装路径 | Playwright ESM 入口（`index.mjs`） |
| `HQ_CHROMIUM` | 本机 chromium-1223 路径 | Chromium 可执行文件 |
| `HQ_SID` | 内置的真实会话 id | 线上冒烟用的会话（只读检查） |

Playwright 与 Chromium 的默认路径是本机的绝对路径；换机器时用环境变量覆盖即可。
