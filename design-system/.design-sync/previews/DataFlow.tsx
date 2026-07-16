import { DataFlow, color, font } from 'huangque-design-system'

// DataFlow 是全屏底纹背景（position:absolute inset:0）：必须放进 position:relative + 固定高度
// + overflow:hidden 的深色容器里，前景内容用 position:relative 叠在上层。结构照搬 WebGLParticles.tsx。

/** 数据管道底纹：金色光点沿正弦曲线流动，像线索在获客管道里跑 */
export function Pipeline() {
  return (
    <div
      style={{
        position: 'relative',
        height: 280,
        borderRadius: 16,
        overflow: 'hidden',
        fontFamily: font.sans,
        background: `radial-gradient(130% 120% at 50% 18%, ${color.panel}, ${color.bg})`,
        border: `1px solid ${color.line}`,
      }}
    >
      <DataFlow />
      <div style={{ position: 'relative', padding: 30 }}>
        <div style={{ fontSize: 12, letterSpacing: '.08em', textTransform: 'uppercase', color: color.gold, fontWeight: 700 }}>
          黄雀 AI · 获客引擎
        </div>
        <div style={{ fontSize: 26, fontWeight: 850, color: color.txt, margin: '10px 0 8px' }}>
          数据流 · 线索在管道里跑
        </div>
        <div style={{ fontSize: 14, color: color.txtDim, maxWidth: 360, lineHeight: 1.6 }}>
          关键词 → 抖音搜视频 → 扒评论区 → 意图过滤 → 精准客户名单
        </div>
      </div>
    </div>
  )
}

/** 密集流线变体：lines/perLine 调高 + speed 加快，做满版科技感底纹 */
export function DenseFlow() {
  return (
    <div
      style={{
        position: 'relative',
        height: 240,
        borderRadius: 16,
        overflow: 'hidden',
        fontFamily: font.sans,
        background: color.bg,
        border: `1px solid ${color.line}`,
      }}
    >
      <DataFlow lines={9} perLine={5} speed={1.8} />
      <div style={{ position: 'relative', padding: 28 }}>
        <div style={{ fontSize: 20, fontWeight: 800, color: color.txt }}>lines = 9 · perLine = 5 · speed = 1.8</div>
        <div style={{ fontSize: 13, color: color.txtDim, marginTop: 8 }}>高密度数据流底纹 · 适合区块/卡片背景</div>
      </div>
    </div>
  )
}

/** 青色 tint 变体：tint 自定义为青色，呼应 ASR 口播 / 数字人节点 */
export function CyanTint() {
  return (
    <div
      style={{
        position: 'relative',
        height: 220,
        borderRadius: 16,
        overflow: 'hidden',
        fontFamily: font.sans,
        background: `radial-gradient(120% 120% at 80% 20%, ${color.panel2}, ${color.bg})`,
        border: `1px solid ${color.line}`,
      }}
    >
      <DataFlow lines={6} perLine={4} tint={color.cyan} />
      <div style={{ position: 'relative', padding: 28 }}>
        <div style={{ fontSize: 12, letterSpacing: '.08em', textTransform: 'uppercase', color: color.cyan, fontWeight: 700 }}>
          ASR · 数字人口播
        </div>
        <div style={{ fontSize: 22, fontWeight: 820, color: color.txt, marginTop: 10 }}>tint = 青色 · 内容生产管线</div>
      </div>
    </div>
  )
}
