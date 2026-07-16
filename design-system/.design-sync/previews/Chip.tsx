import type { ReactNode } from 'react'
import { Chip, color, font } from 'huangque-design-system'

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
        gap: 10,
        flexWrap: 'wrap',
      }}
    >
      {children}
    </div>
  )
}

/** 渠道筛选行：一组 chip 中恰好一个选中（金色实心） */
export function FilterRow() {
  return (
    <Stage>
      <Chip active>全部</Chip>
      <Chip>抖音</Chip>
      <Chip>小红书</Chip>
      <Chip>视频号</Chip>
      <Chip>已成交</Chip>
    </Stage>
  )
}

/** 选中态 vs 未选中态对照：金色实心 / 描边低调 */
export function States() {
  return (
    <Stage>
      <Chip active>已成交客户</Chip>
      <Chip>待跟进线索</Chip>
    </Stage>
  )
}

/** 意图标签筛选：按客户意图过滤名单，"高意向"为当前选中 */
export function IntentFilter() {
  return (
    <Stage>
      <Chip>全部意图</Chip>
      <Chip active>高意向</Chip>
      <Chip>咨询价格</Chip>
      <Chip>同行</Chip>
      <Chip>已拉黑</Chip>
    </Stage>
  )
}
