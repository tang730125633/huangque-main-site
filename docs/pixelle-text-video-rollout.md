# 文案成片上线检查

文案成片由独立功能开关 `pixelle_text_video` 控制，默认关闭。入口、模板接口和付费提交都采用失败关闭策略；只有开关已开启且生成服务 `/health` 返回 `{"status":"healthy"}` 时，侧栏入口才会显示。

## 上线前

1. 在生成服务器部署并启动 Pixelle-Video API，主站通过 `PIXELLE_API_URL` 访问它。生产环境建议使用回环地址，例如 `http://127.0.0.1:8103`。
2. 在主站服务器确认服务健康：

   ```bash
   curl -fsS http://127.0.0.1:8103/health
   ```

   返回值必须包含 `"status":"healthy"`。
3. 保持 `pixelle_text_video` 关闭，先用已登录账号请求能力接口：

   ```bash
   curl -fsS --cookie "hq_session=<session>" \
     https://huangquechuanmei.com/api/gen/text-video/capability
   ```

   开关关闭时应返回 `available: false`，且侧栏不显示“文案成片”。
4. 在管理后台“接单与定价 -> 功能开关”中开启“文案成片”。等待最多 5 秒后再次检查能力接口，必须同时返回 `enabled: true`、`ready: true` 和 `available: true`。
5. 用测试账号分别提交“主题创作”和“完整文案”。完整文案以空行分段，页面显示的画面数、扣点明细中的 `scene_count` 和生成结果画面数必须一致。

## 回滚

先在管理后台关闭 `pixelle_text_video`。关闭后新请求不会扣点或创建任务，已进入队列的任务不受影响。确认能力接口返回 `available: false` 后，再停止生成服务或回滚代码。
