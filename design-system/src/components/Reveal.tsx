import React from 'react'
import { motion } from 'framer-motion'

export interface RevealProps {
  children: React.ReactNode
  /** 入场方向 */
  from?: 'up' | 'down' | 'left' | 'right' | 'scale'
  /** 延迟秒 */
  delay?: number
  /** 位移幅度 px */
  distance?: number
  once?: boolean
  className?: string
  style?: React.CSSProperties
}

/**
 * 滚动入场：元素进入视口时淡入 + 位移/缩放。Framer Motion whileInView 驱动，
 * 自动尊重 prefers-reduced-motion（无位移直接显示）。
 */
export function Reveal({ children, from = 'up', delay = 0, distance = 28, once = true, className, style }: RevealProps) {
  const offset: Record<string, { x?: number; y?: number; scale?: number }> = {
    up: { y: distance },
    down: { y: -distance },
    left: { x: distance },
    right: { x: -distance },
    scale: { scale: 0.92 },
  }
  const o = offset[from]
  return (
    <motion.div
      className={className}
      style={style}
      initial={{ opacity: 0, ...o }}
      whileInView={{ opacity: 1, x: 0, y: 0, scale: 1 }}
      viewport={{ once, amount: 0.25 }}
      transition={{ duration: 0.6, delay, ease: [0.22, 1, 0.36, 1] }}
    >
      {children}
    </motion.div>
  )
}

export default Reveal
