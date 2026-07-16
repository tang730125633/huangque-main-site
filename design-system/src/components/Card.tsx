import React from 'react'
import { color, radius } from '../tokens'

export interface CardProps extends React.HTMLAttributes<HTMLDivElement> {
  /** 是否金色高亮（推荐/主卡） */
  accent?: boolean
  pad?: number
}

/** 黄雀面板卡：暗色卡片化容器，accent 时金色高亮边 */
export function Card({ accent, pad = 18, style, children, ...rest }: CardProps) {
  return (
    <div
      style={{
        background: accent
          ? `linear-gradient(135deg, rgba(231,178,76,.10), ${color.panel})`
          : color.panel,
        border: `1px solid ${accent ? 'rgba(231,178,76,.3)' : color.lineSoft}`,
        borderRadius: radius.lg,
        padding: pad,
        ...style,
      }}
      {...rest}
    >
      {children}
    </div>
  )
}

export default Card
