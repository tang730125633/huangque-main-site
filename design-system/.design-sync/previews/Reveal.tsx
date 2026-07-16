import type { ReactNode } from 'react'
import { Reveal, Tag, color, font } from 'huangque-design-system'

// 黄雀是暗色系统，预览卡底色为白 —— 每个 story 放在深色台面上才是真实观感。
// Reveal 用 whileInView 驱动：元素进入视口即动画到「已显示」终态，所以静态截图捕到的是
// 淡入位移完成后的最终内容（opacity:1、无偏移），这是正确的。
function Stage({ children }: { children: ReactNode }) {
  return (
    <div
      style={{
        background: color.bg,
        borderRadius: 16,
        padding: 32,
        fontFamily: font.sans,
        color: color.txt,
      }}
    >
      {children}
    </div>
  )
}

function StepCard({ tone, tag, title, body }: { tone: 'gold' | 'cyan' | 'green' | 'blue'; tag: string; title: string; body: string }) {
  return (
    <div
      style={{
        background: color.panel,
        border: `1px solid ${color.line}`,
        borderRadius: 14,
        padding: 18,
      }}
    >
      <Tag tone={tone}>{tag}</Tag>
      <div style={{ fontSize: 16, fontWeight: 800, margin: '10px 0 5px', color: color.txt }}>{title}</div>
      <div style={{ fontSize: 13, lineHeight: 1.6, color: color.txtDim }}>{body}</div>
    </div>
  )
}

/** 四个方向入场：获客流水线四步，分别从 上/左/右/缩放 入场（截图为已显示终态） */
export function Directions() {
  return (
    <Stage>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        {/* 四个 from 方向各演示一步；delay 留默认 0 —— 静态截图捕的是已显示终态，
            stagger 在静态里看不出来，零延迟可确保截图时每块都已淡入到位。 */}
        <Reveal from="up">
          <StepCard tone="gold" tag="step 1 · 发现" title="关键词搜视频" body="按行业词在抖音/小红书检索高相关视频，圈定潜客聚集地。" />
        </Reveal>
        <Reveal from="left">
          <StepCard tone="cyan" tag="step 2 · 采集" title="评论区扒线索" body="批量采集评论与互动，沉淀昵称、诉求、联系线索。" />
        </Reveal>
        <Reveal from="right">
          <StepCard tone="green" tag="step 3 · 过滤" title="AI 意图打分" body="语义识别真实需求，筛掉同行与闲逛，保留高意向。" />
        </Reveal>
        <Reveal from="scale">
          <StepCard tone="blue" tag="step 4 · 转化" title="名单回传成交" body="精准客户名单回传飞书，团队跟进直接触达。" />
        </Reveal>
      </div>
    </Stage>
  )
}

/** 标题区入场 + 卡片网格错峰入场（stagger delay，截图为已显示终态） */
export function Headline() {
  const feats = [
    { tone: 'gold' as const, tag: '获客', title: '关键词获客', body: '一句关键词，跑出一份精准客户名单。' },
    { tone: 'cyan' as const, tag: '内容', title: 'AI 作图 / 口播', body: '数字人口播 + AI 配图，内容流水线化。' },
    { tone: 'green' as const, tag: '增效', title: '降本增效', body: '把重复获客动作交给智能体，人只做决策。' },
  ]
  return (
    <Stage>
      <Reveal from="up">
        <div style={{ fontSize: 12, letterSpacing: '.08em', textTransform: 'uppercase', color: color.gold, fontWeight: 700 }}>
          黄雀 · AI 内容工作台
        </div>
        <div style={{ fontSize: 26, fontWeight: 850, color: color.txt, margin: '8px 0 20px' }}>
          滚动入场的首屏叙事
        </div>
      </Reveal>
      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
        {feats.map((f) => (
          <Reveal key={f.title} from="up" style={{ flex: '1 1 180px' }}>
            <StepCard tone={f.tone} tag={f.tag} title={f.title} body={f.body} />
          </Reveal>
        ))}
      </div>
    </Stage>
  )
}
