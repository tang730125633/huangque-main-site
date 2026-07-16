import type { ReactNode } from 'react'
import { Card, Tag, color, font } from 'huangque-design-system'

// 黄雀是暗色系统，预览卡底色为白 —— 每个 story 放在深色台面上才是真实观感。
function Stage({ children }: { children: ReactNode }) {
  return (
    <div style={{ background: color.bg, borderRadius: 16, padding: 28, fontFamily: font.sans, color: color.txt }}>
      {children}
    </div>
  )
}

/** 默认面板卡：标题 + 正文 + 内嵌语义标签 */
export function Default() {
  return (
    <Stage>
      <Card style={{ maxWidth: 360 }}>
        <Tag tone="cyan">评论区线索</Tag>
        <div style={{ fontSize: 18, fontWeight: 800, margin: '12px 0 6px' }}>抖音获客名单</div>
        <div style={{ fontSize: 14, color: color.txtDim, lineHeight: 1.65 }}>
          关键词搜视频 → 扒评论区 → 意图过滤，沉淀出可直接触达的精准客户名单。
        </div>
      </Card>
    </Stage>
  )
}

/** Accent 金色高亮卡：用于推荐 / 主卡，左上金色渐变边 */
export function Accent() {
  return (
    <Stage>
      <Card accent style={{ maxWidth: 360 }}>
        <Tag tone="gold">推荐方案</Tag>
        <div style={{ fontSize: 18, fontWeight: 800, margin: '12px 0 6px' }}>数字人口播 · 一条龙</div>
        <div style={{ fontSize: 14, color: color.txtDim, lineHeight: 1.65 }}>
          正脸图 + 配音脚本 → AI 对口型出片，3 分钟产出可投放的真人口播视频。
        </div>
      </Card>
    </Stage>
  )
}

/** 卡片网格：accent 主卡与普通卡并列，体现获客工作台的模块布局 */
export function Grid() {
  return (
    <Stage>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 14 }}>
        <Card accent>
          <Tag tone="gold">核心</Tag>
          <div style={{ fontSize: 15, fontWeight: 800, margin: '10px 0 5px' }}>关键词获客</div>
          <div style={{ fontSize: 13, color: color.txtDim, lineHeight: 1.6 }}>抖音 / 小红书全网搜，评论区批量采集。</div>
        </Card>
        <Card>
          <Tag tone="green">已上线</Tag>
          <div style={{ fontSize: 15, fontWeight: 800, margin: '10px 0 5px' }}>意图过滤</div>
          <div style={{ fontSize: 13, color: color.txtDim, lineHeight: 1.6 }}>AI 判别真实需求，剔除同行与水军。</div>
        </Card>
        <Card>
          <Tag tone="blue">内容侧</Tag>
          <div style={{ fontSize: 15, fontWeight: 800, margin: '10px 0 5px' }}>AI 作图</div>
          <div style={{ fontSize: 13, color: color.txtDim, lineHeight: 1.6 }}>批量生成口播封面与种草图文。</div>
        </Card>
      </div>
    </Stage>
  )
}
