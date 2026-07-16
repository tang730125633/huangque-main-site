import type { ReactNode } from 'react'
import { Button, color, font } from 'huangque-design-system'

// 黄雀是暗色系统，预览卡底色为白 —— 每个 story 放在深色台面上才是真实观感。
function Stage({ children }: { children: ReactNode }) {
  return (
    <div
      style={{
        background: color.bg,
        borderRadius: 16,
        padding: 28,
        fontFamily: font.sans,
        display: 'flex',
        alignItems: 'center',
        gap: 14,
        flexWrap: 'wrap',
      }}
    >
      {children}
    </div>
  )
}

/** 三种变体：金色实心 CTA / 描边 ghost / 低调 soft */
export function Variants() {
  return (
    <Stage>
      <Button variant="gold">进入工作台</Button>
      <Button variant="ghost">查看演示</Button>
      <Button variant="soft">了解更多</Button>
    </Stage>
  )
}

/** 三种尺寸 sm / md / lg */
export function Sizes() {
  return (
    <Stage>
      <Button variant="gold" size="sm">小号</Button>
      <Button variant="gold" size="md">中号</Button>
      <Button variant="gold" size="lg">大号</Button>
    </Stage>
  )
}

/** 带图标的 CTA 组合 + 禁用态 */
export function WithIconAndDisabled() {
  return (
    <Stage>
      <Button variant="gold" size="lg">进入工作台 →</Button>
      <Button variant="ghost" size="lg">⚡ 一键获客</Button>
      <Button variant="soft" disabled>处理中…</Button>
    </Stage>
  )
}
