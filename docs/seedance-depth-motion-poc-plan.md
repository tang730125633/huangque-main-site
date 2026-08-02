# Seedance 动作模仿隔离 POC 计划

更新时间：2026-08-02

## 结论

先验证“授权人物素材 + 原始动作视频”，不要先建设深度模型服务。Seedance 2.0 官方多模态接口已支持同一请求组合参考图片与参考视频；深度视频没有专用 `depth_control` 参数，只能作为 `reference_video` 的软结构参考。

## 现有能力与缺口

主站已经具备：账号与点数、异步任务、失败退款、Seedance 官方适配器、多密钥池、最多 9 张参考图、安全暂存、轮询恢复、成片下载和资产入库。

当前缺口：`server/content_domains/video_seedance.py` 只构造 `image_url`，没有构造官方支持的 `video_url` / `reference_video`。本 POC 先补适配器合同和隔离脚本，不修改正式用户页面、数据库结构或生产开关。

## 官方合同（2026-08-02 核验）

- Seedance 2.0 多模态参考支持 0–9 张图、0–3 段视频、0–3 段音频及文本组合。
- 参考视频字段为 `type=video_url`、`role=reference_video`；只接受公网 URL 或受信任素材 ID。
- 单段参考视频为 2–15 秒，最多 3 段且总时长不超过 15 秒；支持 MP4/MOV、H.264/H.265、24–60 FPS、单段不超过 200 MB。
- 输出为 4–15 秒；Standard 支持 480p/720p/1080p，Fast 不支持 1080p。
- 含真人脸的直接上传素材受限制。真人身份参考必须先完成人像授权并使用 `asset://asset-...`；不能把普通公网真人照片当成已授权素材。
- 官方定价会因输入视频时长、输出时长和分辨率变化。BytePlus 当前 16:9、5 秒输出示例中，含 2–15 秒视频输入的 Standard 约为 480p US$0.39–0.86、720p US$0.84–1.86、1080p US$2.06–4.57；中国火山方舟账号实际价格和权限必须在付费前现场核对。

官方依据：

- <https://docs.byteplus.com/en/docs/modelark/1520757>
- <https://docs.byteplus.com/en/docs/ModelArk/2291680>
- <https://docs.byteplus.com/docs/ModelArk/1099320>
- <https://www.volcengine.com/docs/82379/2315856?lang=en>

## 三组对照

| 模式 | 输入 | 目的 | 当前状态 |
|---|---|---|---|
| A / `rgb` | 授权人物图 + 原始动作视频 | 建立动作还原、身份污染和成本基线 | POC 已支持 |
| B / `depth` | 授权人物图 + 黑白深度视频 | 判断能否降低原演员和背景污染 | POC 已支持；深度生成器未建设 |
| C / `depth_scene` | 授权人物图 + 深度视频 + 场景图 | 同时锁人物、动作和新场景 | POC 已支持；深度生成器未建设 |

## 执行顺序

1. 用 5 秒、单人全身、固定镜头、简单背景素材跑 A；先用 480p、无声，避免不必要费用。
2. 只有 A 出现明显原演员/背景污染时，才离线生成同片深度视频并跑 B。
3. B 动作可用但场景不稳定时再跑 C。
4. 每组只提交一次，记录官方 task ID、请求参数、`usage.completion_tokens`、耗时、实际扣费和成片 URL；不得因页面无响应重复提交。
5. 人工按 1–5 分评估动作相似度、节奏、身份、脸、服装、身体、手脚、背景污染、黑白泄漏和闪烁。

## 隔离 POC 用法

默认只打印请求，不调用模型：

```bash
python3 scripts/seedance_motion_poc.py \
  --mode rgb \
  --identity-image asset://asset-authorized-person \
  --motion-video https://example.com/motion.mp4
```

真实提交会产生费用，必须在 Tang 确认单条预估成本后，同时显式传 `--submit` 并设置 `SEEDANCE_MOTION_POC_ALLOW_PAID=1`。API Key 仍只从现有受保护环境或密钥池读取，不写入命令、文档或 Git。

## 验收与后续门槛

本阶段完成条件：请求合同测试通过、三种模式可重复构造、默认零费用、真人授权边界明确、现有 Seedance 图片参考不回归。

进入正式产品页前还需要：

- 为本地 MP4 增加受控对象存储上传和任务终态清理；不要把大视频 Base64 放进 JSON。
- 将参考视频接入现有 `xiaole_video` 任务、点数预扣/退款、恢复与资产记录，而不是直接在浏览器调用供应商。
- 至少完成一组三组真实对照，并证明动作质量、单条成本和成功率达到产品门槛。
- 深度方案确有增益后，再选择视频深度模型与时间一致性处理；否则不建设该服务。
