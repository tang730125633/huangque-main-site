import type { ReactNode } from 'react'
import { Tag, Card, color, font } from 'huangque-design-system'

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

/** 六种语义色：金 / 青 / 绿 / 蓝 / 红 / 灰，对应获客场景的状态标注 */
export function Tones() {
  return (
    <Stage>
      <Tag tone="gold">高意向</Tag>
      <Tag tone="cyan">评论区线索</Tag>
      <Tag tone="green">已成交</Tag>
      <Tag tone="blue">抖音</Tag>
      <Tag tone="red">已流失</Tag>
      <Tag tone="muted">待跟进</Tag>
    </Stage>
  )
}

/** 渠道来源组：多平台标签并排，用于名单行的来源标注 */
export function Channels() {
  return (
    <Stage>
      <Tag tone="blue">抖音</Tag>
      <Tag tone="red">小红书</Tag>
      <Tag tone="cyan">视频号</Tag>
      <Tag tone="gold">私域</Tag>
    </Stage>
  )
}

/** 实战上下文：标签嵌入名单卡片，标注客户的渠道与意图状态 */
export function InContext() {
  return (
    <Stage>
      <Card style={{ maxWidth: 340 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
          <Tag tone="blue">抖音</Tag>
          <Tag tone="gold">高意向</Tag>
          <Tag tone="green">已加微</Tag>
        </div>
        <div style={{ fontSize: 15, fontWeight: 800, color: color.txt }}>客户 · 美业门店老板</div>
        <div style={{ fontSize: 13, color: color.txtDim, marginTop: 4, lineHeight: 1.6 }}>
          评论区咨询「同城获客怎么做」，意图过滤判为高意向，已进入跟进队列。
        </div>
      </Card>
    </Stage>
  )
}
