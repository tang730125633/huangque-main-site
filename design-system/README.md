# 黄雀 AI · 设计系统（huangque-design-system）

React + TypeScript 的黄雀 AI 内容工作台前端。视觉以根目录 `DESIGN.md` 为准：暖白运营台、墨绿主操作、铜色成本提示，强调任务、异常、成本与飞书交付。

`src/App.tsx` 是可交互的今日工作台示例，`src/tokens.ts` 与 `src/components/*` 可继续供 **Claude Design `/design-sync`** 同步。

## 跑起来看
```bash
cd design-system
npm install
npm run dev      # Vite 会输出本地预览地址
npm run build    # TypeScript + 生产构建验证
```

## 同步给 Claude Design
```bash
cd design-system
claude
› /design-sync
```
完成后出现在你组织的「Design systems for everyone」里。

## 当前页面

- 今日行动：失败任务、作品审核、获客名单、飞书绑定。
- 实时队列：图片、视频、获客、文案任务状态与进度。
- 经营视图：生产线健康度、本周成本、飞书交付状态。
- 响应式：桌面侧栏、平板双列、手机底部导航。

## 组件库
| 组件 | 说明 |
|---|---|
| `tokens` | 颜色/字体/圆角/间距/阴影（与 `DESIGN.md` 同源）|
| `Button` | gold / ghost / soft |
| `Card` / `GlassCard` | 面板卡 / **玻璃拟态**（backdrop-blur + 高光边）|
| `TiltCard` | **3D 倾斜卡**（Framer Motion，光标透视 + 高光跟随）|
| `Reveal` | **滚动入场**（Framer whileInView，含 reduced-motion）|
| `StatCard` | 指标卡（图标徽章 + 数值 + 涨跌）|
| `Chip` / `Tag` | 筛选 chip / 语义标签 |
| `ParticleField` | 金色粒子**星网**（canvas，鼠标交互，轻量）|
| `DataFlow` | **数据流**（金色光点沿正弦线流动）|
| `WebGLParticles` | **WebGL 3D 粒子球**（Three.js / R3F）|
| `GlowHero` | 首屏（WebGL 粒子背景 + 径向金光 + 标题/CTA）|

历史动效组件仍保留供其他页面复用；当前工作台只使用帮助理解状态变化的轻量动效，并尊重 `prefers-reduced-motion`。
