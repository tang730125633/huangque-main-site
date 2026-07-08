# 工单 · 资产库批量下载 / 批量删除（ASSET-BATCH）

> 供其他 agent 独立执行。先读《工单-其他agent实施》§0 +《任务看板》+ `DESIGN.md`。
> **冲突组：C（`site/workbench/assets.html`）+ A（`server/content_api.py` / `server/content_domains/**`）** —— 抢 `lock/C` + `lock/A`。
> 来源：Issue #408（yuelei-dev）。

## 背景
资产库（图片/音频/视频/数字人形象）目前仅支持逐条操作，管理大量资产效率极低。需要多选 + 批量下载 + 批量删除。

## 改动
### 前端 `site/workbench/assets.html`
- 复用现有多选框架（已有点击选中样式基础），加 checkbox 多选模式：全选 / 取消全选、显示选中数量。
- 「批量下载」：调后端批量打包接口，得到 ZIP 直链/流下载。
- 「批量删除」：弹确认对话框 → 调后端批量删除 → 刷新列表。四态（空/加载/错误/成功）按 DESIGN 规范。

### 后端 `server/content_api.py`（+ 必要时 content_domains）
- 批量下载：新增接口收资产 ID 列表，后端**鉴权归属校验**（只允许下载本人资产，复用 `/api/gen/file` 归属逻辑）后流式打包 ZIP 返回。注意大文件/视频走 COS，ZIP 内可放 COS 直链清单或服务端代拉打包（择一，注意内存，建议流式）。
- 批量删除：新增接口收 ID 列表，**软删除**（status=deleted，不物理删），逐条鉴权归属。为后续回收站预留。
- 退点/计费不涉及（下载删除不扣点）。

## 验收标准
1. 资产库可多选、全选/取消、显示选中数。
2. 批量下载得到含所选资产的 ZIP；**不能下载到别人的资产**（越权返回 403/过滤）。
3. 批量删除走软删除，列表即时刷新；删除的资产从列表消失但库中 status=deleted 可查。
4. 单条操作原有功能不受影响。

## 部署
前端 ship `site/`；后端 ship `server/content_api.py`(+domains) → 重启 `huangque-content`。改 workbench 页记得 `python scripts/stamp_assets.py`。
