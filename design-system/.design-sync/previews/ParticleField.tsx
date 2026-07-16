import { ParticleField, Button, color, font } from 'huangque-design-system'

// 背景组件：放进定位容器（position:relative + 固定高度 + overflow:hidden + 深色底），
// ParticleField 以 position:absolute inset:0 铺满容器画 canvas 金色粒子星网，
// 真实前景内容（眉标/标题/CTA）用 position:relative 叠在上层。
/** 金色粒子星网背景，首屏标题 / CTA 叠在上层 */
export function Starfield() {
  return (
    <div
      style={{
        position: 'relative',
        height: 320,
        borderRadius: 16,
        overflow: 'hidden',
        fontFamily: font.sans,
        background: `radial-gradient(130% 120% at 50% 24%, ${color.panel2}, ${color.bg})`,
      }}
    >
      <ParticleField />
      <div style={{ position: 'relative', padding: 32 }}>
        <div style={{ fontSize: 12, letterSpacing: '.08em', textTransform: 'uppercase', color: color.gold, fontWeight: 700 }}>
          黄雀 · AI 获客
        </div>
        <div style={{ fontSize: 28, fontWeight: 850, color: color.txt, margin: '10px 0 8px' }}>
          关键词进，客户名单出
        </div>
        <div style={{ fontSize: 14, color: color.txtDim, maxWidth: 420, lineHeight: 1.6, marginBottom: 18 }}>
          金色粒子星网铺底，营造作战台氛围；内容稳稳叠在上层。
        </div>
        <Button variant="gold" size="lg">进入工作台 →</Button>
      </div>
    </div>
  )
}

/** 青色 tint + 更密星网（tint=cyan, count=120, 连线更近） */
export function CyanDenseTint() {
  return (
    <div
      style={{
        position: 'relative',
        height: 260,
        borderRadius: 16,
        overflow: 'hidden',
        fontFamily: font.sans,
        background: color.bg,
      }}
    >
      <ParticleField tint={color.cyan} count={120} linkDist={150} opacity={0.6} />
      <div style={{ position: 'relative', padding: 28 }}>
        <div style={{ fontSize: 12, letterSpacing: '.08em', textTransform: 'uppercase', color: color.cyan, fontWeight: 700 }}>
          数据流 · 实时
        </div>
        <div style={{ fontSize: 22, fontWeight: 850, color: color.txt, margin: '8px 0 4px' }}>
          tint=青 · count=120 密星网
        </div>
        <div style={{ fontSize: 13, color: color.txtDim }}>线索在后台持续汇入。</div>
      </div>
    </div>
  )
}

/** 静态非交互变体（interactive=0，纯氛围底，鼠标不推开粒子） */
export function StaticAmbient() {
  return (
    <div
      style={{
        position: 'relative',
        height: 260,
        borderRadius: 16,
        overflow: 'hidden',
        fontFamily: font.sans,
        background: `radial-gradient(120% 120% at 80% 0%, rgba(231,178,76,.10), transparent 55%), ${color.bg}`,
      }}
    >
      <ParticleField interactive={0} speed={0.6} opacity={0.45} />
      <div style={{ position: 'relative', padding: 28 }}>
        <div style={{ fontSize: 12, letterSpacing: '.08em', textTransform: 'uppercase', color: color.gold, fontWeight: 700 }}>
          氛围底 · 不抢内容
        </div>
        <div style={{ fontSize: 22, fontWeight: 850, color: color.txt, margin: '8px 0 4px' }}>
          interactive=0 静态星网
        </div>
        <div style={{ fontSize: 13, color: color.txtDim, maxWidth: 360, lineHeight: 1.6 }}>
          作为卡片/区块的低调底纹，粒子缓慢漂移、不响应鼠标。
        </div>
      </div>
    </div>
  )
}
