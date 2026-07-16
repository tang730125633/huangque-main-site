## 黄雀 conventions — read before building

**This is a dark, gold-accent design system.** Every screen must sit on a dark surface — use `window.HuangQue.color.bg` (`#070b13`) as the page background. Components are dark-themed and look broken on a white/light background. The accent is 黄雀金 `color.gold` (`#e7b24c`).

**No CSS classes, no stylesheet to author against.** This DS is inline-style / token based: there is no class vocabulary (`styles.css` is a runtime stub). You style via (1) each component's own props, and (2) the exported **token object** for your own layout glue. Both components and tokens live on `window.HuangQue`.

**No provider or context is required** — every component is self-contained. Just mount and use. All animated components respect `prefers-reduced-motion`.

### Token vocabulary (`window.HuangQue.{color,font,radius,space,shadow,gradient}`)
- `color`: `bg #070b13`, `panel`, `panel2`, `surface`, `surface2`, `line`, `lineSoft`, `lineStrong`, `txt #eaf1fa`, `txtDim`, `txtFaint`, `gold #e7b24c`, `goldSoft`, `cyan`, `blue`, `green`, `warn`, `red`
- `font`: `sans`, `mono` (use `mono` for numbers/metrics)
- `radius`: `sm 8 · md 11 · lg 14 · xl 18 · xxl 22 · pill 999`
- `space`: `xs 6 · sm 10 · md 14 · lg 18 · xl 24 · xxl 32`
- `shadow`: `card · gold · pop` · `gradient`: `gold · goldGlow`

### Component idiom (style via props, not classes)
- `Button` — `variant: 'gold' | 'ghost' | 'soft'`, `size: 'sm' | 'md' | 'lg'`. `gold` = primary CTA.
- `Card` — dark panel container; `accent` = gold-highlighted (use for the main/recommended card).
- `GlassCard` — glass panel (`blur`, `glow`); place over content/gradient so the blur shows.
- `StatCard` — metric tile: `value`, `label`, `hint`, `change`, `changeBad` (red for down), `icon`, `iconTone: 'gold'|'cyan'|'blue'|'green'|'red'`, `accent`.
- `Chip` — filter pill; `active` = selected (gold). `Tag` — semantic label; `tone: 'gold'|'cyan'|'green'|'blue'|'red'|'muted'`.
- `TiltCard` — cursor-tilt card (`max`, `lift`). `Reveal` — scroll-in wrapper (`from: 'up'|'down'|'left'|'right'|'scale'`, `delay`, `distance`).
- **Background components** `ParticleField` (canvas star-net), `DataFlow` (flowing dots), `WebGLParticles` (3D sphere): put inside a `position:relative` + fixed-height + `overflow:hidden` dark box, with your content in a `position:relative` div above them.
- `GlowHero` — full first-screen hero (`title` required, `eyebrow`, `subtitle`, `children` for CTAs, `bg: 'webgl'|'none'`, `height`); it self-backgrounds, render it directly.

### Where the truth lives
Per component: `components/general/<Name>/<Name>.prompt.md` (usage + example JSX) and `<Name>.d.ts` (full prop types). Read these before composing a component.

### Idiomatic snippet
```jsx
const { GlowHero, Button, StatCard, color, font } = window.HuangQue;
<div style={{ background: color.bg, color: color.txt, fontFamily: font.sans, minHeight: '100vh' }}>
  <GlowHero eyebrow="黄雀 AI" title={<>评论区获客，<span style={{ color: color.gold }}>AI 内容</span>成交。</>}
            subtitle="关键词 → 抖音搜视频 → 扒评论区 → 意图过滤 → 精准客户名单。">
    <Button variant="gold" size="lg">进入工作台 →</Button>
    <Button variant="ghost" size="lg">看演示</Button>
  </GlowHero>
  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 14, padding: 24 }}>
    <StatCard accent icon="🎯" value="1,284" label="今日获客线索" change="▲ 较昨日 19.8%" />
  </div>
</div>
```
