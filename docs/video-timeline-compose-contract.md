# `video-timeline-compose` 底层契约与 Agent 接线边界

## 一句话结论

`video-timeline-compose` 把当前账号已有的图片、视频、文字卡、可选配音和可选 BGM 按时间轴合成一条 1080p 视频。它复用 `matrix_template_video` 的报价、确认、幂等、任务队列、失败退款、状态轮询和视频资产交付，不新建第二套付费系统，也不依赖修改 `fang` 渲染服务。

## 能力入口

- CLI capability：`video-timeline-compose`
- 服务端 generation kind：`matrix_template_video`
- 提交端点：`POST /api/gen/matrix-template`
- 权限：报价需要 `generation:quote`；确认提交还需要 `generation:submit`
- 任务状态：拿到 `job_id` 后只调用现有 `task`
- CLI 最低版本：`0.15.6`

## 输入

```json
{
  "segments": [
    {"type":"image","asset_id":101,"asset_index":0,"duration":3,"transition":"fade"},
    {"type":"video","asset_id":202,"trim_start":0,"trim_end":5,"transition":"fade"},
    {"type":"text_card","text":"把 AI 变成真正能交付结果的人","style":"full-overlay-bold","duration":3,"transition":"none"}
  ],
  "ratio":"9:16",
  "preserve_source_audio":true,
  "bgm":false
}
```

可选配音：

```json
{
  "voiceover": {
    "text":"完整口播文案，最多 120 字",
    "voice":"从 voices 的 ready 项复制 voice_key",
    "voice_scope":"public",
    "speed":1.0
  }
}
```

可选 BGM：

```json
{
  "bgm":true,
  "bgm_asset_id":303,
  "bgm_volume":0.18
}
```

`bgm=true` 必须指定当前账号自己的音频资产。系统不会替用户随便选择平台音乐，避免版权不明和跨账号素材串用。

## 段落规则

- 共 2–20 段。
- `image`：当前账号已完成图片任务的 `asset_id`；多图任务用 `asset_index` 选择第几张，默认 0；时长 1–10 秒。
- `video`：当前账号已完成视频资产的 `asset_id`；`trim_start/trim_end` 按秒；裁剪后最多 60 秒。
- `text_card`：1–120 字；首版样式只有 `full-overlay-bold`；时长 1–8 秒。
- `transition` 表示“当前段进入下一段”的方式，仅支持 `none/fade`；最后一段必须为 `none`。
- 总时长最多 180 秒；比例支持 `9:16 / 16:9 / 1:1`。
- 报价会再次读取素材归属、文件存在性、视频真实时长和文件版本；确认时任何变化都会要求重新报价。

## 报价与确认

首次调用只报价：

```bash
hq run video-timeline-compose --input @timeline.json --json
```

报价公式：

```text
总价 = 基础费 5 点 + 每段 1 点 + 每 30 秒 2 点（不足 30 秒按一档）
```

返回 `quote_token`、`cost` 和 `cost_breakdown`。用户确认后，必须用完全相同的 `timeline.json` 再提交一次：

```bash
hq run video-timeline-compose --input @timeline.json \
  --confirm --quote-token '<quote_token>' --json
```

不得自动确认、不得替换素材或修改裁剪后继续复用旧报价。响应不确定时只使用相同参数和原报价恢复，不创建新任务。

## 执行与失败边界

- FFmpeg 把每段统一为目标分辨率、30fps、H.264/AAC，再做 `fade` 或硬切拼接。
- 文字卡使用服务器配置的中文字体；生产默认 `/home/ubuntu/.fonts/NotoSansSC.ttf`。
- 原视频音轨由 `preserve_source_audio` 控制；配音和本人 BGM 在最终阶段混音。
- 渲染失败会在同一 Job 内自动重试一次；仍失败进入现有错误终态和自动退款，不创建第二个 Job。
- 服务重启后，已创建 Job 继续走现有 `matrix_template_submission` 和 pending/running 恢复边界；不会重复扣点。

## DeepSeek 只负责的接线

1. `hq-compose` 子 Agent 学会在用户明确要“多张图/多段视频拼起来”时选择本能力。
2. 先调用 `assets` 获取当前账号的图片、视频、音频 ID，不能猜 ID。
3. 信息不足时逐项询问素材顺序、每段时长/裁剪、转场、比例、是否保留原声、是否配音/BGM。
4. 组装 `segments` 后先免费报价，把素材顺序和 `cost_breakdown` 做成确认卡。
5. 只有用户明确确认才复用同一输入和 `quote_token` 提交一次。
6. 保存 `job_id`，只用 `task` 查询原任务；最终视频必须回到对话并能播放/下载。
7. 神秘顾客先跑免费路径：自然语言识别 → 素材选择 → 报价卡；付费成片需要老板单独确认。

DeepSeek 不需要修改主站 Content、CLI、定价、幂等、退款或 FFmpeg；这些属于本契约的底层边界。
