# Wan 优先、Seedance 对照的动作模仿隔离 POC 计划

更新时间：2026-08-02

## 结论

第一候选改为阿里云百炼 `wan2.2-animate-mix`。它是专门的视频换人模型，直接接收“授权人物图片 + 原始表演视频”，替换原视频主角并保留场景、光照、色调、动作和表情，不需要提示词，也不要求用户先生成深度视频。`wan2.2-animate-move` 只适合按人物图片背景生成动作视频，不满足“锁住原视频场景”的目标。Seedance 2.0 保留为第二候选和深度软参考对照。

截图中的“先生成黑白深度视频再交给 Wan”属于可实验的社区工作流，不是官方 Animate Move API 的必要步骤。先直传原始动作视频；只有真实结果证明身份或背景污染明显时，才测试深度路线。

## 现有能力与缺口

主站已经具备：账号与点数、异步任务、失败退款、Seedance 官方适配器、多密钥池、最多 9 张参考图、安全暂存、轮询恢复、成片下载和资产入库。

当前缺口：主站没有 Wan 视频换人调用；Seedance 适配器此前也没有构造官方支持的 `video_url` / `reference_video`。本 POC 增加两个默认零费用的隔离入口，不修改正式用户页面、数据库结构或生产开关。

## Wan 官方合同（2026-08-02 核验）

- 模型固定为 `wan2.2-animate-mix`，输入字段是 `image_url` 与 `video_url`。
- 参考视频支持 MP4/AVI/MOV、2–30 秒、单文件不超过 200 MB；人物图不超过 5 MB。
- `wan-std` 适合低成本预览，`wan-pro` 更平滑但更慢、更贵。
- 华北 2（北京）原价为 std ¥0.6/输出秒、pro ¥0.9/输出秒；5 秒估算分别为 ¥3、¥4.5。失败调用不收费。
- 任务异步执行，结果 URL 只保留 24 小时，成功后必须下载到现有对象存储。

官方依据：

- <https://help.aliyun.com/zh/model-studio/wan-animate-mix-api>
- <https://help.aliyun.com/zh/model-studio/wan2-2-animate-mix>
- <https://github.com/Wan-Video/Wan2.2>

## 补充工具评估

### Wan-Dancer：不进入指定动作复刻主链

`Wan-Video/Wan-Dancer` 是“人物图 + 音乐 + 舞种提示词 → 原创长舞视频”，不是“人物图 + 目标动作视频 → 原视频人物替换”。它适合以后做一分钟级音乐舞蹈生成，但当前不能替代 `wan2.2-animate-mix`。

- 官方开源协议：Apache-2.0。
- 官方参考环境：Ubuntu 22.04、8 × NVIDIA A800 80GB。
- 当前没有托管推理供应商，不能复用现有百炼 API 直接调用。
- 结论：本阶段不下载 14B 权重、不建设 GPU 集群、不接正式产品。

官方依据：

- <https://github.com/Wan-Video/Wan-Dancer>
- <https://huggingface.co/Wan-AI/Wan-Dancer-14B>

### depthvideo.com：作为可选深度预处理

该工具在浏览器本地使用 Depth Anything V2 / ONNX Runtime Web 逐帧推理，素材不上传，能够导出 MP4/WebM。它不是供应商 API，因此不接后端；需要深度对照时直接人工生成一次即可。

首次深度对照采用：

1. 选择 Depth Anything V2 Small。Small 为 Apache-2.0；Base/Large 为 CC-BY-NC-4.0，不进入黄雀商业链路。
2. 选择“仅人物”、灰度、MP4，保留完整身体；不要使用热力图。
3. 导出后确认时长、宽高、帧率与原视频一致，且手脚没有被人物遮罩切掉。
4. 把深度 MP4 交给 `seedance_motion_poc.py --mode depth`；不要交给官方 `wan2.2-animate-mix`，后者应使用需要保留场景的原始表演视频。
5. 如果人物遮罩闪烁或断肢，再改用“全图”灰度重导一次，不预建新的深度服务。

依据：

- <https://www.depthvideo.com/>
- <https://github.com/DepthAnything/Depth-Anything-V2>
- <https://github.com/DepthAnything/Video-Depth-Anything>

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
| A / Wan std | 授权人物图 + 原始动作视频 | 保留原场景的视频换人基线 | POC 已支持；第一候选 |
| B / Wan pro | 与 A 相同 | 判断更高质量模式是否值得增加 50% 成本 | POC 已支持 |
| C / Seedance RGB | 授权人物素材 + 原始动作视频 | 与通用多模态视频模型对照 | POC 已支持 |
| D / Seedance depth | 授权人物素材 + 黑白深度视频 | 仅在污染明显时验证深度软参考 | POC 已支持；深度生成器未建设 |

## 执行顺序

1. 用 5 秒、单人全身、固定镜头、简单背景素材跑 Wan std，预估 ¥3。
2. 换人正确但画质或稳定性不足时才跑 Wan pro，预估 ¥4.5。
3. 再用同一素材跑 Seedance RGB；只有出现原演员/背景污染时才增加深度对照。
4. 深度对照直接用 depthvideo.com 的 Small 模型生成，不建设服务、不上传素材。
5. 每组只提交一次，记录官方 task ID、请求参数、耗时、实际扣费和成片 URL；不得因页面无响应重复提交。
6. 人工按 1–5 分评估动作相似度、节奏、身份、脸、服装、身体、手脚、背景污染、黑白泄漏和闪烁。

## 隔离 POC 用法

Wan 默认只打印请求和官方原价估算，不调用模型：

```bash
python3 scripts/wan_motion_poc.py \
  --identity-image-url https://example.com/authorized-person.jpg \
  --motion-video-url https://example.com/motion.mp4 \
  --expected-seconds 5
```

真实 Wan 提交必须由 Tang 确认费用和人物授权，然后同时传 `--submit --confirm-authorized-person` 并设置 `WAN_MOTION_POC_ALLOW_PAID=1`。

Seedance 对照同样默认不调用模型：

```bash
python3 scripts/seedance_motion_poc.py \
  --mode rgb \
  --identity-image asset://asset-authorized-person \
  --motion-video https://example.com/motion.mp4
```

真实提交会产生费用，必须在 Tang 确认单条预估成本后，同时显式传 `--submit` 并设置 `SEEDANCE_MOTION_POC_ALLOW_PAID=1`。API Key 仍只从现有受保护环境或密钥池读取，不写入命令、文档或 Git。

## 验收与后续门槛

本阶段完成条件：Wan 与 Seedance 请求合同测试通过、默认零费用、真人授权边界明确、现有 Seedance 图片参考不回归。

进入正式产品页前还需要：

- 为本地 MP4 增加受控对象存储上传和任务终态清理；不要把大视频 Base64 放进 JSON。
- 将参考视频接入现有 `xiaole_video` 任务、点数预扣/退款、恢复与资产记录，而不是直接在浏览器调用供应商。
- 至少完成一组三组真实对照，并证明动作质量、单条成本和成功率达到产品门槛。
- 深度方案确有增益后，再选择视频深度模型与时间一致性处理；否则不建设该服务。
