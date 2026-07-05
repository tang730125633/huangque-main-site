# 工单 · nano banana 作图产出走 COS 直链

> 来自 tang：发现独立作图服务 `imggen_api`（nano banana，:8101，`/api/gen/banana`）的出图还是本地链接，没跟 COS 全产出直链一起接上。补齐它。冲突组 **imggen（server/imggen_api.py）**。前端无需改。

## 背景
COS 全产出直链（PR #104）只接了 `content_domains`（image/audio/video/tryon），但 nano banana 作图走的是**独立服务** `imggen_api.py`，出图 `urls = "/api/gen/file/"+f` 仍是本地。

## 改动（只改 server/imggen_api.py）
- 加自包含 COS 上传（不跨 import content_domains，保持 imggen 组隔离）：`_cos_enabled()` + `_cos_get_client()`（`qcloud_cos` 懒加载）+ `_public_url(rel, content_type)`——COS 配置齐全且文件存在 → 上传返回直链；未配置/失败 → 回退本地 `/api/gen/file/`。
- `gen_banana` 出图 `urls` → `_public_url(f, "image/png")`。

## 为什么不需要额外部署前置
- imggen 服务（`huangque-imggen-api`）的 EnvironmentFile **就是** `/home/ubuntu/content-api/content.env`——COS_* 环境变量**已在其中**（PR #99 配的）。
- `qcloud_cos` 已装（服务用 `/usr/bin/python3`，已验证可 import）。
- 所以本 PR 合并部署（`./ship ... server/imggen_api.py` 重启 huangque-imggen-api）后**立即生效**，无需再动服务器配置。

## 验收
1. nano banana 作图（`/api/gen/banana`）新出的图，`url` 为 `https://huangque-media-1435693839.cos.ap-guangzhou.myqcloud.com/huangque/...`，公网可访问。
2. COS 未配置/失败时回退本地，功能不受影响。
3. `py_compile` / `ci_validate` / `unittest` 全过。

> 本单只改后端一个文件，不动前端、不碰服务器。
