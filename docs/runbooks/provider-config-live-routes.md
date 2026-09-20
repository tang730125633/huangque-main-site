# 在线 URL / Key 配置扩展

本次沿用 PostgreSQL 的 provider_config 版本表，不新增迁移、不修改现有 Key。

| 配置目标 | 影响功能 | 免费验证 |
| --- | --- | --- |
| image.seedream | 黄雀引擎 1 标准 / Pro | GET /models |
| image.banana.nb2 | 纳米香蕉 2 / Pro，短剧角色与关键帧 | GET /v1beta/models |
| image.openai | 黄雀引擎 2，数字人口播 AI 配图 | GET /v1/models |
| video.tryon.classic | RunningHub 换装换背景 | POST /uc/openapi/accountStatus（仅查询） |
| video.tryon.fast | WaveSpeed 换装、Seedance AI 超清 | GET /balance |

模型详情的「修改 URL／Key · 版本发布」与更多管理使用同一个编辑器。
草稿 → 免费验证 → 确认启用；不表示已完成真实生成验收。修改输入使旧验证失效。
协议不变，不是任意供应商切换；URL 仍受既有 HTTPS 域名白名单约束。
OpenAI / Gemini 接受尾部 /v1、/v1beta 并归一化，避免重复路径。

新任务在扣点前持久化版本；普通任务、短剧耐久扣费入口、数字人配图均覆盖。
WaveSpeed 恢复使用原 prediction ID 和原配置，不重新提交。
历史无引用的 Gemini / OpenAI / tryon 任务保持旧环境路径。
首次开启时 Gemini / OpenAI 的官方地址、备用地址和 Key 一起冻结；
只有发布后台版本后，才以后台 URL 替代两条地址。配置轮换不修改视频号池。

## 发布

先验证 CI / 隔离 PostgreSQL / 独立审核。精确部署合并 main 的改动文件。
备份这些运行文件及 content.env，核对原文件与父提交一致，否则停止。
admin、content、imggen-api 须使用同一 PostgreSQL、主密钥和以下开关：

```
HQ_PROVIDER_CONFIG_WIRING=image.seedream,image.banana.nb2,image.openai,video.tryon.classic,video.tryon.fast
```

代码首次上线需重启 huangque-admin、huangque-content、huangque-imggen-api；
以后后台发布 URL / Key 无需重启。新开关前核对原凭据非空和基线路由，不能输出秘密。
公共入口未认证的 401 不算登录态验收；需要确认模型详情能打开同一个版本配置表单。
不得用真实付费生成作为默认部署冒烟测试。

回滚：关闭新目标开关（保留 image.seedream），恢复备份代码和环境，再重启受影响服务。
已经固定版本的任务不可用旧 worker 重跑，先确认无在途任务或保留新解析器。
数据库版本不可删除，已有后台配置优先在界面回滚。

免费接口来源：
- https://ai.google.dev/api/models
- https://www.runninghub.cn/runninghub-api-doc-cn/api-425748943
- https://wavespeed.ai/docs/check-balance

范围外：OAuth 重新授权、下架线路、付费生成验证；号池/托管渠道保持已有编辑流程。
