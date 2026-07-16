import React from 'react'
import { radius } from '../tokens'

export interface GlassCardProps extends React.HTMLAttributes<HTMLDivElement> {
  /** 模糊强度 px，默认 18 */
  blur?: number
  /** 金色边缘高光，默认 true */
  glow?: boolean
}

/**
 * 玻璃拟态卡：半透明 + backdrop-blur + 顶部高光边。
 * 放在有内容/渐变的背景上效果最佳（透出底层）。
 */
export function GlassCard({ blur = 18, glow = true, style, children, ...rest }: GlassCardProps) {
  return (
    <div
      style={{
        position: 'relative',
        borderRadius: radius.xl,
        padding: 22,
        background: 'rgba(18,26,42,0.45)',
        backdropFilter: `blur(${blur}px) saturate(140%)`,
        WebkitBackdropFilter: `blur(${blur}px) saturate(140%)`,
        border: '1px solid rgba(255,255,255,0.12)',
        boxShadow: glow
          ? 'inset 0 1px 0 rgba(255,255,255,0.14), 0 18px 50px rgba(0,0,0,0.45)'
          : '0 18px 50px rgba(0,0,0,0.4)',
        overflow: 'hidden',
        ...style,
      }}
      {...rest}
    >
      {glow && (
        <span
          aria-hidden
          style={{ position: 'absolute', top: 0, left: '12%', right: '12%', height: 1, background: 'linear-gradient(90deg,transparent,rgba(231,178,76,.6),transparent)' }}
        />
      )}
      {children}
    </div>
  )
}

export default GlassCard
