import { GlowHero, Button, color } from 'huangque-design-system'

// GlowHero 自带整屏背景（WebGL 金色粒子 + 径向金光 + 标题/副标题/CTA）。
// 不需要外层深色 Stage —— 它本身就是暗色首屏。给它一个 height，传真实内容即可；
// 外层只用 borderRadius + overflow:hidden 把圆角裁干净。

/** 黄雀首屏：eyebrow + 金色高亮标题 + 副标题 + 双 CTA（WebGL 粒子背景） */
export function Hero() {
  return (
    <div style={{ borderRadius: 16, overflow: 'hidden' }}>
      <GlowHero
        height={380}
        eyebrow="黄雀 AI · 内容工作台"
        title={
          <>
            一句关键词，<span style={{ color: color.gold }}>精准客户名单</span>自己跑出来
          </>
        }
        subtitle="抖音搜视频 → 扒评论区 → 意图过滤 → 数字人口播 → 成交。获客全链路交给 AI。"
      >
        <Button variant="gold" size="lg">进入工作台 →</Button>
        <Button variant="ghost" size="lg">看演示</Button>
      </GlowHero>
    </div>
  )
}

/** 无背景变体：bg='none' 关闭 WebGL 粒子，保留径向金光，更轻更稳 */
export function NoBg() {
  return (
    <div style={{ borderRadius: 16, overflow: 'hidden' }}>
      <GlowHero
        height={340}
        bg="none"
        eyebrow="黄雀 AI · 获客引擎"
        title={
          <>
            让 <span style={{ color: color.gold }}>线索</span> 在管道里自己流动
          </>
        }
        subtitle="无 WebGL 的轻量首屏：纯径向金光底，适合低算力 / 优先加载速度的落地页。"
      >
        <Button variant="gold" size="lg">免费试用</Button>
        <Button variant="soft" size="lg">了解方案</Button>
      </GlowHero>
    </div>
  )
}
