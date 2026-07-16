import { useEffect, useRef } from 'react'
import { color } from '../tokens'

export interface ParticleFieldProps {
  /** 粒子数量（按面积自适应，这是上限基准）。默认 70 */
  count?: number
  /** 粒子主色，默认黄雀金 */
  tint?: string
  /** 是否连线成星网。默认 true */
  link?: boolean
  /** 连线最大距离 px。默认 130 */
  linkDist?: number
  /** 鼠标排斥/吸引强度，0 关闭。默认 0.6 */
  interactive?: number
  /** 漂移速度倍率。默认 1 */
  speed?: number
  /** 整体不透明度。默认 0.55 */
  opacity?: number
  className?: string
  style?: React.CSSProperties
}

interface P { x: number; y: number; vx: number; vy: number; r: number }

/**
 * 黄雀 · 粒子星网背景。canvas 全屏铺底，金色粒子缓慢漂移 + 临近连线，
 * 鼠标靠近会轻微推开粒子。尊重 prefers-reduced-motion（静止呈现）。
 * 用法：放在深色容器内，position:absolute inset:0，内容叠在上层。
 */
export function ParticleField({
  count = 70,
  tint = color.gold,
  link = true,
  linkDist = 130,
  interactive = 0.6,
  speed = 1,
  opacity = 0.55,
  className,
  style,
}: ParticleFieldProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const reduced =
      typeof window !== 'undefined' &&
      window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches

    let w = 0
    let h = 0
    let dpr = Math.min(window.devicePixelRatio || 1, 2)
    let parts: P[] = []
    const mouse = { x: -9999, y: -9999 }
    let raf = 0

    const rgb = hexToRgb(tint)

    function resize() {
      const rect = canvas!.getBoundingClientRect()
      w = rect.width
      h = rect.height
      canvas!.width = Math.floor(w * dpr)
      canvas!.height = Math.floor(h * dpr)
      ctx!.setTransform(dpr, 0, 0, dpr, 0, 0)
      const n = Math.min(count, Math.round((w * h) / 14000))
      parts = Array.from({ length: Math.max(12, n) }, () => ({
        x: Math.random() * w,
        y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.35 * speed,
        vy: (Math.random() - 0.5) * 0.35 * speed,
        r: Math.random() * 1.6 + 0.7,
      }))
    }

    function step() {
      ctx!.clearRect(0, 0, w, h)
      ctx!.globalAlpha = opacity

      for (const p of parts) {
        if (!reduced) {
          p.x += p.vx
          p.y += p.vy
          if (interactive) {
            const dx = p.x - mouse.x
            const dy = p.y - mouse.y
            const d2 = dx * dx + dy * dy
            if (d2 < 120 * 120) {
              const d = Math.sqrt(d2) || 1
              const f = ((120 - d) / 120) * interactive
              p.x += (dx / d) * f
              p.y += (dy / d) * f
            }
          }
          if (p.x < 0 || p.x > w) p.vx *= -1
          if (p.y < 0 || p.y > h) p.vy *= -1
          p.x = Math.max(0, Math.min(w, p.x))
          p.y = Math.max(0, Math.min(h, p.y))
        }
        ctx!.beginPath()
        ctx!.fillStyle = `rgba(${rgb},0.9)`
        ctx!.shadowColor = `rgba(${rgb},0.9)`
        ctx!.shadowBlur = 6
        ctx!.arc(p.x, p.y, p.r, 0, Math.PI * 2)
        ctx!.fill()
      }

      if (link) {
        ctx!.shadowBlur = 0
        for (let i = 0; i < parts.length; i++) {
          for (let j = i + 1; j < parts.length; j++) {
            const a = parts[i]
            const b = parts[j]
            const dx = a.x - b.x
            const dy = a.y - b.y
            const dist = Math.sqrt(dx * dx + dy * dy)
            if (dist < linkDist) {
              ctx!.globalAlpha = opacity * (1 - dist / linkDist) * 0.5
              ctx!.strokeStyle = `rgba(${rgb},1)`
              ctx!.lineWidth = 0.6
              ctx!.beginPath()
              ctx!.moveTo(a.x, a.y)
              ctx!.lineTo(b.x, b.y)
              ctx!.stroke()
            }
          }
        }
      }
      raf = reduced ? 0 : requestAnimationFrame(step)
    }

    function onMove(e: MouseEvent) {
      const rect = canvas!.getBoundingClientRect()
      mouse.x = e.clientX - rect.left
      mouse.y = e.clientY - rect.top
    }
    function onLeave() {
      mouse.x = -9999
      mouse.y = -9999
    }

    resize()
    step()
    window.addEventListener('resize', resize)
    if (interactive) {
      canvas.addEventListener('mousemove', onMove)
      canvas.addEventListener('mouseleave', onLeave)
    }
    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('resize', resize)
      canvas.removeEventListener('mousemove', onMove)
      canvas.removeEventListener('mouseleave', onLeave)
    }
  }, [count, tint, link, linkDist, interactive, speed, opacity])

  return (
    <canvas
      ref={canvasRef}
      className={className}
      style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', display: 'block', pointerEvents: interactive ? 'auto' : 'none', ...style }}
      aria-hidden
    />
  )
}

function hexToRgb(hex: string): string {
  const m = hex.replace('#', '')
  const n = m.length === 3 ? m.split('').map((c) => c + c).join('') : m
  const r = parseInt(n.slice(0, 2), 16)
  const g = parseInt(n.slice(2, 4), 16)
  const b = parseInt(n.slice(4, 6), 16)
  return `${r},${g},${b}`
}

export default ParticleField
