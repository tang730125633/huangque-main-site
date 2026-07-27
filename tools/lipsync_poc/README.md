# 短剧口型 Provider PoC 工具

该目录是阶段 0-A 的离线评测框架，不属于生产服务，也不包含任何真实
Provider、密钥、扣点或用户入口。

## 安全边界

- 当前唯一 Provider 是 `mock`，只复制输入视频，不访问网络且不收费。
- 样本媒体不得提交 Git；清单只能引用 `assets_root` 下的相对路径。
- API Key、Token、Cookie、Authorization 和带查询参数的 URL 会从报告中脱敏。
- 默认输出到已被 Git 忽略的 `.local-content-out/lipsync-poc`。
- `reports/` 只保留目录占位文件，真实报告不得进入提交。

## 清单格式

清单必须符合 `sample_manifest.schema.json`。`visible` 样本必须提供
`character_key`；每条样本必须包含无声源视频、项目主音轨、锁定台词、时长、
比例和输出规格。

```json
{
  "manifest_version": "1.0",
  "dataset_name": "internal-baseline-v1",
  "samples": [
    {
      "sample_id": "front-normal-01",
      "video_file": "videos/front-normal-01.mp4",
      "audio_file": "audio/front-normal-01.wav",
      "transcript": "今天我们开始测试口型同步。",
      "speaking_mode": "visible",
      "character_key": "host",
      "face_target": {"type": "character", "value": "host"},
      "duration_ms": 5000,
      "ratio": "9:16",
      "output_spec": {"resolution": "720p", "fps": 25},
      "tags": ["front", "normal-speed"]
    }
  ]
}
```

## 运行

只校验清单、路径和输入哈希，不运行 Provider：

```bash
python -m tools.lipsync_poc.run_poc \
  --manifest C:/private-lipsync/manifest.json \
  --assets-root C:/private-lipsync/assets \
  --validate-only
```

运行离线 Mock 合同流程：

```bash
python -m tools.lipsync_poc.run_poc \
  --manifest C:/private-lipsync/manifest.json \
  --assets-root C:/private-lipsync/assets \
  --provider mock
```

Mock 输出不是口型结果，只用于验证清单、状态机、媒体探测、原子报告和脱敏逻辑。
阶段 0-B 才允许增加真实 Provider Adapter。

## 报告

每个样本按 Provider 隔离生成运行状态、媒体和报告：

```text
<output-dir>/<provider>/
  state/<sample-id>.json
  media/<sample-id>.mp4
  reports/<sample-id>.json
```

状态文件会在提交前保存确定性的 `request_id`，并在 `create_job()` 返回后立即
原子保存 Provider Job ID。进程中断、轮询异常或超时后，不要重新创建付费任务；
使用原输出目录恢复：

```bash
python -m tools.lipsync_poc.run_poc \
  --manifest C:/private-lipsync/manifest.json \
  --assets-root C:/private-lipsync/assets \
  --provider mock \
  --resume
```

对已完成任务重新下载结果：

```bash
python -m tools.lipsync_poc.run_poc \
  --manifest C:/private-lipsync/manifest.json \
  --assets-root C:/private-lipsync/assets \
  --provider mock \
  --refetch
```

恢复时会校验 Provider、样本 ID 和输入哈希；不一致时拒绝操作。普通运行发现
已有状态文件时也会停止，避免重复创建可能收费的任务。

每份报告包含：

- 不可变 `input_hash`
- Provider Job ID 与能力声明
- 输入/输出 FFprobe 结果
- 时长、帧率、分辨率和输出音轨指标
- 待填写的人工盲评字段
- 已脱敏的 Provider 元数据

失败和超时同样会保存报告，包括 Provider Job ID、最后状态、取消结果、费用
待核对状态和恢复能力。支持取消的 Provider 在超时时会调用 `cancel_job()`。

报告不保存原始绝对媒体路径，也不记录环境变量或 Provider 密钥。Cookie、
Set-Cookie、Authorization、Proxy-Authorization、X-API-Key 和 X-Auth-Token
等 HTTP 头即使出现在异常字符串中也会被统一脱敏。
