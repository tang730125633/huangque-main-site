# 模板成片统一任务 API（第一阶段）

## 状态与范围

本分支实现文档中的统一异步任务入口、账号素材解析、核心素材适配和完整 COS
交付门槛。**不是原 Word 全量验收通过的声明。尚未部署，默认关闭。**
依赖字体参数主站 #1679、生成端 #219，以及配套 material_adaptation=auto-v1
生成端和 relay。不得只更新主站就开放入口。

本次没有修改 IP12 Agent、Skill、现有网页或旧 CLI 的调用方式。
旧 `POST /api/gen/matrix-template` 不变。新接口由 content-api 编排已有持久化
任务、计费与退款，生成服务器继续执行渲染。不是让 Agent 直接连接 Worker。

## 接口与鉴权

- `GET /internal/matrix-template/templates`：完整目录、分类和文字微调合同。
- `POST /internal/matrix-template/jobs`：HTTP 202，返回 job_id、query_url、dedupe_key。
- `GET /internal/matrix-template/jobs/{job_id}`：查询本人任务，刷新 COS 签名链接。

两种凭据同时必需：`Authorization: Bearer <用户凭据>` 和
`X-HQ-Internal-Token: <现有服务间凭据>`。不要把内部凭据放到网页或模型上下文。
`account.username` / `account.account_id` 必须与鉴权结果匹配，不能冒充其他账号。

这些路径由 content-api 提供；当前生产 nginx 是否开放该前缀未修改。
同机服务可通过现有 content-api 的 loopback 源站调用；跨机需单独配置受保护路由。

## 请求

```json
{
  "template_id": "ref-04-foshan-yellow-strip",
  "account": {"username": "<实际用户>"},
  "materials": {"mode": "auto"},
  "texts": {"top_text": "团队每天稳定产出短视频", "bottom_text": "评论区扣888"},
  "bgm": true,
  "dedupe_key": "<本次操作的唯一编号>"
}
```

`materials.mode=auto` 优先读取鉴权账号的长期素材索引；没有 account_id 的旧身份仅
使用本人未过期 upload_id。不会跨账号选素材，也不会把 Agent 已生成的成片当原素材。
`picked.ids` 支持32位十六进制 asset_id，以及 `vid_...` / `img_...` 临时 upload_id。

可选 `voiceover: {"text":"口播文案", "voice":"实际音色ID", "speed":1.0}`，
沿用已有音色归属校验及120字上限。配音时默认关闭 BGM，可显式 `bgm:true` 和
`bgm_volume` 开启原有混音。`text_revision` / `text_overrides` 与
[文字微调合同](matrix-template-text-controls.md)一致。

## 素材与恢复

只读索引路径通过 `MATRIX_ACCOUNT_ASSET_ROOT` 配置，默认
`/home/ubuntu/hq-ip-agent/data/assets`。COS 对象相对键必须精确属于
`hq-materials/<已验证account_id>/<asset_id><ext>`，不接受调用方指定任意对象键。
读取同一账号 index.json 后再从 COS 恢复源文件，不依赖原临时上传凭据。
使用主站已有 COS 配置，校验文件长度和索引里的 SHA256；索引缺哈希的旧素材首次读取
后计算哈希并冻结到任务。源选择在下载前持久化，重试不重新抽样。

源不足按顺序复用；短于1.5秒的视频定格补足，其余短视频循环补足；图片生成定长视频。
长视频在全长范围确定性选段，模板仍控制最终裁切区域，不把所有画面先裁为9:16。
单素材解码探测失败时尝试剩余已验证素材，全部不可用才失败。文件归属和哈希检查不能取消。
素材预处理沿用现有 HDR/SDR 色彩编码参数，不添加调色或遮罩。

状态：accepted -> preparing -> rendering -> ready / failed。
准备、配音、渲染、混音、封面和 COS 上传均为后台任务；原持久化任务恢复机制继续生效。
只有最终视频和封面均上传 COS 并核对长度后才 ready。失败沿用主站退款机制。
结果含 duration_seconds、width、height、file_size、cos_key、cover_cos_key、有效链接及到期时间。
素材清单含 asset_id、SHA、原始入点、适配方式、是否复用和跳过的条目。

同一个 dedupe_key 重试始终定位原任务；改制作参数返回409。明确重新生成应换新key。
不传key时对近5分钟活跃任务进行全参数去重，不能保证跨进程长时间断网后的无键重试身份；
正式调用必须持久化并复用 dedupe_key。已失败任务的新操作不需要修改文案。

## 与 Word 尚未对齐的部分

- `materials.mode=refs` 任意 URL/SHA 输入尚未开放；使用账号 asset_id。
- 指定 BGM ID、已合成音频 ID 尚未开放；不能默默忽略这些字段。
- 配音仍沿用现有完整音轨混音方式，未实现“只延长最后一个画面位+0.5秒”和自动 ducking。
- 图片轻推拉、重复素材微位移、循环接缝淡化尚未加入，避免未经视觉验证改变模板。
- 文案目前先按60/80字入口上限截取、记录 adaptations，再走原AI断句；不是完整的
  实际字体宽度自适应算法。显式字号不会静默缩小，排不下仍明确失败。
- 保留既有“仅26号为口播分类”及安全区，不按文档的22/4分类或180px规则改模板。
- 未接入新 CLI 命令、Agent 工具声明或网站入口，未做线上付费调用/多节点真机验收。

上述项目完成且双方验收前，不应宣称整份 Word 已全部交付。

## 开放前

1. 升级生成端 API、matrix_material_adaptation.py、GPU/poller、relay；保护在跑任务。
2. 验证各节点声明 auto-v1，旧节点不能领取新任务；结果必须回显同一协议。
3. 配置只读素材索引与现有COS凭据，确认账号ID、桶及前缀一致。
4. 部署主站模块；按业务确认是否启用 `MATRIX_UNIFIED_API_ENABLED=1`。
5. 从调用方环境使用同一个幂等键实测提交、查询、下载和重试，核对计费与退款。

测试脚本 `tools/matrix_unified_client.py` 从环境读取凭据，不把密钥写入示例文件。
