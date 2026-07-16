import type { ReactNode } from 'react'
import { GlassCard, Tag, color, font } from 'huangque-design-system'

// 玻璃拟态要透出底层才看得出效果 —— 台面放渐变 + 网点做背景。
function GradientStage({ children }: { children: ReactNode }) {
  return (
    <div
      style={{
        position: 'relative',
        borderRadius: 16,
        padding: 36,
        overflow: 'hidden',
        fontFamily: font.sans,
        color: color.txt,
        background: `radial-gradient(620px circle at 18% 8%, rgba(231,178,76,.22), transparent 52%), linear-gradient(135deg, ${color.panel2}, ${color.bg})`,
      }}
    >
      <div
        aria-hidden
        style={{
          position: 'absolute',
          inset: 0,
          backgroundImage: 'radial-gradient(rgba(255,255,255,.10) 1px, transparent 1px)',
          backgroundSize: '22px 22px',
        }}
      />
      <div style={{ position: 'relative' }}>{children}</div>
    </div>
  )
}

/** 玻璃卡（默认）：高光边 + 强模糊，叠在金色渐变 + 网点上 */
export function OnGradient() {
  return (
    <GradientStage>
      <GlassCard style={{ maxWidth: 360 }}>
        <Tag tone="gold">评论区获客</Tag>
        <div style={{ fontSize: 19, fontWeight: 800, margin: '12px 0 6px' }}>玻璃拟态面板</div>
        <div style={{ fontSize: 14, color: color.txtDim, lineHeight: 1.6 }}>
          半透明 + backdrop-blur，叠在内容/渐变之上透出底层，顶部一道金色高光边。
        </div>
      </GlassCard>
    </GradientStage>
  )
}

/** 弱模糊、无高光边（glow=false, blur=10） */
export function PlainBlur() {
  return (
    <GradientStage>
      <GlassCard glow={false} blur={10} style={{ maxWidth: 320 }}>
        <div style={{ fontSize: 15, fontWeight: 700 }}>无高光 · 弱模糊</div>
        <div style={{ fontSize: 13, color: color.txtDim, marginTop: 6 }}>
          glow=false，blur=10 —— 更克制的玻璃面。
        </div>
      </GlassCard>
    </GradientStage>
  )
}
