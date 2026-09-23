# 模板逐层文字微调接口

本次只开放服务端和 HQ CLI 参数，不修改 IP12 Agent、网页编辑面板或 Skill。
模板文件不被改写。未传参数时，保留既有渲染路径和默认样式。
使用 HQ CLI 调用时需更新到 **0.15.15**；旧版 CLI 会在本地拒绝新字段。直接调用服务端接口不依赖 CLI 安装。

## 1. 查询

调用现有 `matrix-template-controls`，输入 `template_id`。返回新增字段：

```json
{
  "template_id": "ref-04-foshan-yellow-strip",
  "text_tunable": true,
  "text_controls": {
    "contract_version": 1,
    "text_revision": "当前模板、适配实现和字体集合的64位指纹",
    "layers": {
      "top1": {
        "defaults": {},
        "font_size_mode": "fixed",
        "default_size_range_px": [88, 88]
      }
    },
    "fields": {},
    "fonts": [],
    "preview_supported": false
  }
}
```

上述仅为响应结构示例，图层、默认值、字体及范围均以实际查询结果为准。
`tunable` / `template_revision` / `overrides_schema` 属于旧版 v05 微调合同；
**不要用 `tunable=false` 判断新文字微调不可用，应读取 `text_tunable`。**

`layers` 是当前模板实际支持的文字层：参考模板通常有 `top1/top2/top3/bottom2`，
九宫格为 `top_text/bottom_text`，动效模板使用各自字段名。
第26套为 `main_title/subtitle/caption_zh/caption_en`，标题样式覆盖该角色的全部阶段。
不存在的层不能传；空文案对应层仍为空，不会因为微调而新增内容。

## 2. 生成

在原有 `matrix-template-generate` 或 `matrix-template-batch-generate` 输入上增加：

```json
{
  "text_revision": "原样复制 text_controls.text_revision",
  "text_overrides": {
    "top1": {
      "font_family": "从接口返回的字体列表选择",
      "font_size_px": 96,
      "color": "#FFE023",
      "offset_x_px": 0,
      "offset_y_px": -20,
      "stroke_width_px": 6,
      "stroke_color": "#101010"
    },
    "bottom2": {"font_size_px": 72}
  }
}
```

继续使用原有标题、行动文案、本人素材、音色和报价确认流程。本示例省略这些原有必填字段，
不是可以单独提交的完整请求。参数加入报价/幂等输入后，确认提交必须保留同一份输入。

| 字段 | 含义与范围 |
|---|---|
| `font_family` | 接口返回的已安装字体名，不接受文件路径或 URL |
| `font_size_px` | 16–240 的整数像素；显式指定后不再自动缩小 |
| `color` | `#RRGGBB` 文字填充色 |
| `offset_x_px` | -60–60，基于模板默认位置，正数右移 |
| `offset_y_px` | -60–60，基于模板默认位置，正数下移 |
| `stroke_width_px` | 0–24 像素，可为小数；0 关闭文字描边 |
| `stroke_color` | `#RRGGBB` 描边色 |

坐标以 1080×1920 画布为准。描边指 `-webkit-text-stroke`，不改变模板已有阴影、底色、转场或动效。
只传需要修改的字段，不传的字段使用模板默认值。删除覆盖字段或传空对象即恢复默认。
默认字号为 `auto` 的模板会返回原始自动适配范围；`defaults.font_size_px` 是默认上限，不代表每次成片都取上限。
参数范围不是任意文案都能排入的保证；超宽、增加越界或重叠会明确失败，不忽略参数、不降级字体。

当前 `text_overrides` 用于正式生成，不与旧版 `overrides` / `preview_id` 混用，
也不使用旧版“两版对比预览”接口。旧版 v05 预览保持不变。

## 3. 重试与结果

每个任务冻结覆盖参数、字体身份和模板版本，互不修改全局默认值。
结果返回 `text_revision` 和 `text_overrides`；主站核对回显一致后才交付。
请求结果不确定时沿用原任务/幂等键，禁止重新抽样式或重新创建收费任务。

生成节点须有相同模板、微调实现和字体集合，并升级 GPU 样式应用/布局检查组件及协议 v2 poller。
未升级或版本不一致的节点只能接原任务，不能接微调任务。没有兼容节点时明确提示不可用。

## 4. 发布顺序与边界

1. 合并审查通过后，在服务 Python 中安装生成仓库 `deploy/requirements-matrix-text-controls.txt`。
2. 升级上游预检/目录 API，以及空闲节点的生成 API、`matrix_text_controls.py`、GPU helper/runtime，保持模板源和完整字体集合一致；不要中断在跑任务。接口 `fonts[].sha256` 可用于核对字体差异。
3. 部署本 PR 的 relay 与 poller，确认协议 v2 和文字能力心跳；旧普通任务仍可由旧节点处理。
4. 部署主站两个 content domain 模块、`hq_cli_api.py`，更新 HQ CLI；Agent 同事据本合同独立接入。
5. 验证只读参数查询、错误参数不建单、确认后的单条/批量生成和结果回显。

没有执行线上部署、服务重启、真实用户扣费或付费配音/ASR/翻译调用。
