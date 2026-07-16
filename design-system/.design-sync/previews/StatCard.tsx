import type { ReactNode } from 'react'
import { StatCard, color, font } from 'huangque-design-system'

function Stage({ children }: { children: ReactNode }) {
  return (
    <div style={{ background: color.bg, borderRadius: 16, padding: 28, fontFamily: font.sans }}>
      {children}
    </div>
  )
}

/** 获客看板：图标徽章 + 大数值 + 较昨日涨跌（含跌幅 changeBad 红色） */
export function Metrics() {
  return (
    <Stage>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 14 }}>
        <StatCard icon="🎯" iconTone="gold" value="1,284" label="今日获客线索" hint="抖音 + 小红书评论区" change="▲ 较昨日 19.8%" />
        <StatCard icon="💬" iconTone="cyan" value="62.4%" label="意图过滤准确率" change="▲ 较上周 4.2%" />
        <StatCard icon="🎬" iconTone="blue" value="48" label="数字人口播产出" hint="本周累计" change="▼ 较上周 6.0%" changeBad />
      </div>
    </Stage>
  )
}

/** 主指标高亮（accent 金色数值）+ 负向指标对照 */
export function AccentAndDown() {
  return (
    <Stage>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 14, maxWidth: 540 }}>
        <StatCard accent icon="💰" iconTone="gold" value="¥86,400" label="本月成交额" hint="AI 内容成交闭环" change="▲ 较上月 32%" />
        <StatCard icon="📉" iconTone="red" value="3.1%" label="线索流失率" change="▲ 较上月 0.7%" changeBad />
      </div>
    </Stage>
  )
}

/** 单卡最简形态（无图标、无涨跌） */
export function Minimal() {
  return (
    <Stage>
      <div style={{ maxWidth: 220 }}>
        <StatCard value="7,920" label="累计触达客户" />
      </div>
    </Stage>
  )
}
