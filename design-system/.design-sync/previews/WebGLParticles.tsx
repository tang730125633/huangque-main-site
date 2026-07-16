import { WebGLParticles, Button, color, font } from 'huangque-design-system'

// 全屏背景组件：放进定位容器（position:relative + 固定高度），内容叠在上层。
/** WebGL 3D 金色粒子球做首屏背景，标题/CTA 叠在上层 */
export function Sphere() {
  return (
    <div
      style={{
        position: 'relative',
        height: 320,
        borderRadius: 16,
        overflow: 'hidden',
        fontFamily: font.sans,
        background: `radial-gradient(130% 120% at 50% 28%, ${color.panel}, ${color.bg})`,
      }}
    >
      <WebGLParticles />
      <div style={{ position: 'relative', padding: 30 }}>
        <div style={{ fontSize: 12, letterSpacing: '.08em', textTransform: 'uppercase', color: color.gold, fontWeight: 700 }}>
          WebGL · Three.js / R3F
        </div>
        <div style={{ fontSize: 26, fontWeight: 850, color: color.txt, margin: '10px 0 16px' }}>3D 粒子球背景</div>
        <Button variant="gold" size="lg">进入工作台 →</Button>
      </div>
    </div>
  )
}

/** 青色粒子变体（tint 自定义） */
export function CyanTint() {
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
      <WebGLParticles count={1800} tint={color.cyan} />
      <div style={{ position: 'relative', padding: 28 }}>
        <div style={{ fontSize: 20, fontWeight: 800, color: color.txt }}>tint = 青色 · count = 1800</div>
      </div>
    </div>
  )
}
