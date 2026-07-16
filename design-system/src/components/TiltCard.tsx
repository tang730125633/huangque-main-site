import React, { useRef } from 'react'
import { motion, useMotionValue, useSpring, useTransform } from 'framer-motion'
import { color, radius } from '../tokens'

export interface TiltCardProps extends React.HTMLAttributes<HTMLDivElement> {
  /** 最大倾斜角度，默认 10 */
  max?: number
  /** 悬停上浮 px，默认 6 */
  lift?: number
}

/**
 * 3D 倾斜卡：鼠标在卡上移动 → 卡片按光标做透视倾斜 + 金色高光跟随。
 * Framer Motion 弹簧驱动，松手回正。
 */
export function TiltCard({ max = 10, lift = 6, style, children, ...rest }: TiltCardProps) {
  const ref = useRef<HTMLDivElement>(null)
  const mx = useMotionValue(0.5)
  const my = useMotionValue(0.5)
  const rotX = useSpring(useTransform(my, [0, 1], [max, -max]), { stiffness: 200, damping: 18 })
  const rotY = useSpring(useTransform(mx, [0, 1], [-max, max]), { stiffness: 200, damping: 18 })
  const glareX = useTransform(mx, [0, 1], ['0%', '100%'])
  const glareY = useTransform(my, [0, 1], ['0%', '100%'])

  function onMove(e: React.MouseEvent) {
    const r = ref.current!.getBoundingClientRect()
    mx.set((e.clientX - r.left) / r.width)
    my.set((e.clientY - r.top) / r.height)
  }
  function onLeave() {
    mx.set(0.5)
    my.set(0.5)
  }

  return (
    <motion.div
      ref={ref}
      onMouseMove={onMove}
      onMouseLeave={onLeave}
      whileHover={{ y: -lift }}
      style={{
        rotateX: rotX,
        rotateY: rotY,
        transformPerspective: 900,
        transformStyle: 'preserve-3d',
        position: 'relative',
        borderRadius: radius.xl,
        padding: 22,
        background: color.panel,
        border: `1px solid ${color.lineStrong}`,
        boxShadow: '0 18px 50px rgba(0,0,0,.45)',
        overflow: 'hidden',
        cursor: 'pointer',
        ...(style as any),
      }}
      {...(rest as any)}
    >
      <motion.span
        aria-hidden
        style={{
          position: 'absolute',
          inset: 0,
          background: useTransform([glareX, glareY], ([x, y]) => `radial-gradient(420px circle at ${x} ${y}, rgba(231,178,76,.16), transparent 45%)`),
          pointerEvents: 'none',
        }}
      />
      <div style={{ transform: 'translateZ(40px)' }}>{children}</div>
    </motion.div>
  )
}

export default TiltCard
