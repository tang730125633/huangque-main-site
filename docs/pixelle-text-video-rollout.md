# 文案成片上线检查

文案成片由独立功能开关 `pixelle_text_video` 控制，默认关闭。入口、模板接口和付费提交都采用失败关闭策略；只有开关已开启且生成服务 `/health` 返回 `{"status":"healthy"}` 时，侧栏入口才会显示。

本文是上线前检查清单，不表示相关代码已经合并、部署或在生产环境启用。

## 口播素材契约

- 生成服务器必须先提供与 Task 4 一致的口播素材契约，包括 `/api/avatar-assets`、`talking_material` 请求字段、任务 phase 和 `talking_warnings`。主站完成回归测试不代表生成服务器依赖已经可用；依赖未部署并验证前，必须保持功能开关关闭。
- 页面开关默认关闭。旧客户端不传 `talking_material`，或请求中的 `talking_material.enabled` 不是 `true` 时，主站按 `{"enabled": false}` 处理，原有公共音色、个人音色和付费任务路径保持兼容。
- 规划阶段以每个口播分镜约 6 秒为目标，用所选语速估算并显示到 0.1 秒。完整文案优先按句末标点拆分，短句合并，长句按逗号或字符安全拆分，通常保持在 3–9 秒；不得截断、改写或遗漏原文，最终提交必须精确使用用户确认的分镜文本和选择。
- Linux CI 仍需单独关注路径大小写、Node.js 页面 runtime 测试环境和平台相关文件行为。Windows 本地通过不能替代 Linux CI；合并前必须确认 Linux CI 的 Python 套件、`node --check` 和静态检查全部通过。
- 合并与部署是两个独立授权动作。测试完成和文档就绪不授权 push、创建 PR、合并、发布、部署或开启生产功能开关；每一步都必须获得对应的单独批准。

## 上线前

1. 先在生成服务器部署并启动包含上述口播素材契约的 Pixelle-Video API，再考虑开启主站功能。主站通过 `PIXELLE_API_URL` 访问它；地址必须按实际网络边界配置，不应假定主站与生成服务位于同一主机。图片模板使用 `PIXELLE_MEDIA_WORKFLOW`，视频模板必须单独配置 `PIXELLE_VIDEO_WORKFLOW`，默认值为 `runninghub/video_wan2.1_fusionx.json`。
2. 在主站服务器确认服务健康：

   ```bash
   curl -fsS "$PIXELLE_API_URL/health"
   ```

   返回值必须包含 `"status":"healthy"`。
3. 保持 `pixelle_text_video` 关闭，先用已登录账号请求能力接口：

   ```bash
   curl -fsS --cookie "hq_session=<session>" \
     https://huangquechuanmei.com/api/gen/text-video/capability
   ```

   开关关闭时应返回 `available: false`，且侧栏不显示“文案成片”。
4. 在管理后台“接单与定价 -> 功能开关”中开启“文案成片”。等待最多 5 秒后再次检查能力接口，必须同时返回 `enabled: true`、`ready: true` 和 `available: true`。
5. 用测试账号分别提交“主题创作”和“完整文案”。完整文案保留用户空行边界，并在每个段落内按语义和所选语速自动拆成约 6 秒分镜；页面显示的画面数、扣点明细中的 `scene_count` 和生成结果画面数必须一致。

## 回滚

先在管理后台关闭 `pixelle_text_video`。关闭后新请求不会扣点或创建任务，已进入队列的任务不受影响。确认能力接口返回 `available: false` 后，再停止生成服务或回滚代码。
