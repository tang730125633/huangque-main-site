import type { ReactNode } from 'react'
import { TiltCard, Tag, color, font } from 'huangque-design-system'

// 黄雀是暗色系统，预览卡底色为白 —— 每个 story 放在深色台面上才是真实观感。
// 注意：TiltCard 的 3D 倾斜 + 金色高光是「鼠标悬停跟随」的交互态，静态截图呈现的是
// 松手回正后的「静止态」，这是正确的；这里把静止态也排成完整、上品相的卡。
function Stage({ children }: { children: ReactNode }) {
  return (
    <div
      style={{
        background: `radial-gradient(620px circle at 22% 0%, rgba(231,178,76,.10), transparent 55%), ${color.bg}`,
        borderRadius: 16,
        padding: 32,
        fontFamily: font.sans,
        color: color.txt,
        display: 'flex',
        gap: 18,
        flexWrap: 'wrap',
        alignItems: 'stretch',
        justifyContent: 'center',
      }}
    >
      {children}
    </div>
  )
}

/** 默认：单张 3D 倾斜卡，内含标签 + 标题 + 正文 + 数据脚注（静止态） */
export function Default() {
  return (
    <Stage>
      <TiltCard style={{ maxWidth: 360 }}>
        <Tag tone="gold">关键词获客</Tag>
        <div style={{ fontSize: 20, fontWeight: 850, margin: '14px 0 8px', color: color.txt }}>
          抖音评论区线索引擎
        </div>
        <div style={{ fontSize: 14, lineHeight: 1.65, color: color.txtDim }}>
          输入行业关键词 → 自动搜视频、扒评论、过滤意图，沉淀一份可直接触达的精准客户名单。
        </div>
        <div style={{ display: 'flex', gap: 22, marginTop: 18, paddingTop: 16, borderTop: `1px solid ${color.lineSoft}` }}>
          <div>
            <div style={{ fontSize: 22, fontWeight: 850, color: color.gold }}>1,284</div>
            <div style={{ fontSize: 12, color: color.txtFaint, marginTop: 2 }}>本周新增线索</div>
          </div>
          <div>
            <div style={{ fontSize: 22, fontWeight: 850, color: color.cyan }}>38%</div>
            <div style={{ fontSize: 12, color: color.txtFaint, marginTop: 2 }}>高意向占比</div>
          </div>
        </div>
      </TiltCard>
    </Stage>
  )
}

/** 三张并排倾斜卡：内容工作台的三大能力（静止态，悬停各自独立倾斜） */
export function FeatureGrid() {
  const items = [
    { tone: 'gold' as const, tag: '获客', title: '关键词获客', body: '抖音/小红书搜视频，批量扒评论区潜客。' },
    { tone: 'cyan' as const, tag: '内容', title: '数字人口播', body: '一张正脸图 + 文案，秒出真人感口播视频。' },
    { tone: 'green' as const, tag: '转化', title: '意图过滤', body: 'AI 打分筛掉同行与闲逛，只留可成交线索。' },
  ]
  return (
    <Stage>
      {items.map((it) => (
        <TiltCard key={it.title} max={14} style={{ flex: '1 1 200px', maxWidth: 240 }}>
          <Tag tone={it.tone}>{it.tag}</Tag>
          <div style={{ fontSize: 17, fontWeight: 800, margin: '12px 0 6px', color: color.txt }}>{it.title}</div>
          <div style={{ fontSize: 13, lineHeight: 1.6, color: color.txtDim }}>{it.body}</div>
        </TiltCard>
      ))}
    </Stage>
  )
}
