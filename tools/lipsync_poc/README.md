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

每个样本生成一份 JSON，包含：

- 不可变 `input_hash`
- Provider Job ID 与能力声明
- 输入/输出 FFprobe 结果
- 时长、帧率、分辨率和输出音轨指标
- 待填写的人工盲评字段
- 已脱敏的 Provider 元数据

报告不保存原始绝对媒体路径，也不记录环境变量或 Provider 密钥。
