# Design System — AI 内容工作台

## Product Context

- **What this is:** 面向美业获客和数字人交付的 AI 内容工作台，把文案、抓取、图片、音频、数字人视频、资产库、飞书 bot 和成本看板串成一个可运营系统。
- **Who it's for:** Tang 和小方这样的 AI 运营者、交付团队成员，以及后续被授权进入的客户/伙伴。
- **Space/industry:** 美业获客、短视频内容生产、AI 员工系统、数字人口播交付。
- **Project type:** 工作台式 Web app / 运营 dashboard / 任务生产系统，不是营销 landing page。

## Aesthetic Direction

- **Direction:** Industrial / Utilitarian with warm creative accents.
- **Decoration level:** Intentional. 用极少量材质感和状态色表达“内容生产中”，不要大面积渐变、装饰球、营销式 hero。
- **Mood:** 像一个安静、可靠、能赚钱的内容工厂。页面第一眼应该让运营者知道今天该处理什么、哪个任务失败、哪个客户成本最高。
- **Reference:** Timarsky 星空的「工具聚合 + 资产 + 积分」结构可参考，但视觉上我们要更克制、更经营化。

## Typography

- **Display / Page Title:** Satoshi or local fallback `PingFang SC` — 用于页面标题和关键数字，现代但不花哨。
- **Body:** Source Sans 3 + `PingFang SC` — 长中文说明、表单和任务列表优先保证可读性。
- **UI / Labels:** Same as body, 12-14px，标签和按钮文字保持紧凑。
- **Data / Tables:** Geist Mono or JetBrains Mono — 用于成本、任务 ID、时长、token、失败码，必须启用 `font-variant-numeric: tabular-nums`。
- **Code:** JetBrains Mono.
- **Loading:** 第一版可用系统字体；上线产品再自托管 `Source Sans 3` / `Geist Mono`，避免 Google Fonts 在国内加载不稳。

### Type Scale

| Token | Size | Usage |
|---|---:|---|
| `text-xs` | 12px | 状态、辅助信息、表格次要列 |
| `text-sm` | 13px | 表单标签、卡片元信息 |
| `text-base` | 14px | 默认正文、按钮、输入 |
| `text-md` | 16px | 面板标题 |
| `text-lg` | 20px | 页面分区标题 |
| `text-xl` | 24px | 页面标题 |
| `display` | 32px | 关键指标数字，不用于普通卡片 |

## Color

**Approach:** Balanced and operational. 色彩主要承担状态、成本和能力分类，不做一整页单色主题。

### Light Mode

| Token | Hex | Usage |
|---|---|---|
| `bg` | `#F6F7F2` | 页面背景，偏暖白 |
| `surface` | `#FFFFFF` | 工具面板、表格、模态框 |
| `surface-muted` | `#EEF2EC` | 次级区块、上传区 |
| `text` | `#191C1A` | 主文字 |
| `text-muted` | `#68736D` | 辅助文字 |
| `line` | `#D9DED6` | 分割线、边框 |
| `primary` | `#176B5B` | 主要操作、在线、稳定 |
| `secondary` | `#B86B2B` | 成本、商业指标、充值 |
| `accent` | `#2F6FED` | 链接、任务流转、飞书绑定 |
| `creative` | `#8A5CF6` | 创意类能力小面积点缀，禁止做大面积背景 |
| `success` | `#16803C` | 成功 |
| `warning` | `#B7791F` | 注意、排队、成本偏高 |
| `error` | `#C2413A` | 失败、泄露风险、任务异常 |
| `info` | `#2563EB` | 提示 |

### Dark Mode

| Token | Hex | Usage |
|---|---|---|
| `bg` | `#101412` | 深色页面背景 |
| `surface` | `#171D1A` | 主面板 |
| `surface-muted` | `#202822` | 次级面板 |
| `text` | `#ECF1EA` | 主文字 |
| `text-muted` | `#9AA69F` | 辅助文字 |
| `line` | `#2E3832` | 边框 |

Dark mode 要降低饱和度，状态色不直接照搬亮色。

## Spacing

- **Base unit:** 4px.
- **Density:** Compact but breathable. 运营后台要高信息密度，但不能像日志堆。
- **Scale:** `2xs=2`, `xs=4`, `sm=8`, `md=16`, `lg=24`, `xl=32`, `2xl=48`, `3xl=64`.
- **Panel padding:** 16px desktop, 12px mobile.
- **Table row height:** 40-48px.
- **Toolbar height:** 44px.

## Layout

- **Approach:** Grid-disciplined app layout.
- **Default Shell:** left nav + top context bar + main workspace + optional right inspector.
- **Desktop grid:** `240px sidebar / minmax(720px, 1fr) main / 320px inspector`.
- **Tablet:** sidebar collapses to icon rail, inspector becomes drawer.
- **Mobile:** bottom nav + single-column task flow; heavy creation pages should encourage desktop use.
- **Max content width:** Dashboard 1440px, creation panels 1280px.
- **Border radius:** `sm=4px`, `md=6px`, `lg=8px`, `full=9999px`. Cards default 6-8px; no over-rounded bubbly UI.

## Components

- **Navigation:** icon + label, current route with left accent bar.
- **Command Buttons:** use icon buttons for tool actions where possible; text buttons only for explicit commands like `立即生成`, `提交任务`, `保存项目`.
- **Segmented Controls:** 用于能力类型、模型、比例、任务状态。
- **Upload Zones:** stable aspect-ratio boxes; show accepted types and size limits.
- **Task Cards:** fixed-height compact rows with status, cost, duration, owner, retry action.
- **Asset Cards:** thumbnail + type badge + source + linked task.
- **Tables:** sticky header, tabular numbers, row action menu.
- **Right Inspector:** 显示当前项目上下文、预计消耗、飞书群绑定、最近失败。
- **Cost Chip:** secondary/copper color，所有生成按钮附近必须显示预计消耗。

## Motion

- **Approach:** Minimal-functional.
- **Duration:** micro 80ms, short 160ms, medium 240ms.
- **Easing:** enter `cubic-bezier(.16,1,.3,1)`, exit `cubic-bezier(.7,0,.84,0)`.
- **Use:** 页面切换、抽屉、任务状态更新、上传完成、错误提醒。
- **Avoid:** 大面积滚动动画、背景动效、炫技转场。

## Design Principles

1. **先让运营者知道今天该干什么。** Dashboard 第一屏必须有今日行动、失败任务、成本异常、客户状态。
2. **每个生成动作都显示成本。** 不允许隐藏积分/人民币估算。
3. **素材、作品、交付分开。** 素材给 AI 用，作品给运营筛，交付给客户看。
4. **飞书是交付现场，网页是中控台。** 页面要显示 bot/群/任务状态，但不要试图替代飞书聊天。
5. **少做营销页，多做工作流。** 首屏直接进入工作台。

## Decisions Log

| Date | Decision | Rationale |
|---|---|---|
| 2026-06-25 | 新建设计系统 | 为网页化 bot / AI 内容工作台建立一致的前端设计源文件 |
| 2026-06-25 | 选择克制运营后台风格 | 产品服务一人运营和团队交付，需要可扫视、可复盘、可控成本 |
| 2026-06-25 | 明确不复制竞品紫色娱乐化风格 | 竞品偏创意工具，我们要更像可交付的美业获客内容中台 |
| 2026-06-28 | 设计系统 Demo 对齐工作台方向 | 移除暗色粒子营销 Hero，落地暖白运营台，并补齐今日行动、任务队列、能力健康度、成本和飞书交付视图 |
| 2026-06-28 | 登录页与工作台统一 | 保留现有鉴权逻辑，使用深墨绿业务叙事区与暖白表单区，移动端收敛为专注登录的单列布局 |
