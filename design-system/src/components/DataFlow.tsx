import { useEffect, useRef } from 'react'
import { color } from '../tokens'

export interface DataFlowProps {
  /** 流线条数，默认 5 */
  lines?: number
  /** 每条线上的流动光点数，默认 3 */
  perLine?: number
  tint?: string
  speed?: number
  className?: string
  style?: React.CSSProperties
}

/**
 * 数据流：横向多条正弦曲线，金色光点沿线流动（像数据/线索在管道里跑）。
 * 适合做卡片/区块的科技感底纹。尊重 reduced-motion（静止）。
 */
export function DataFlow({ lines = 5, perLine = 3, tint = color.gold, speed = 1, className, style }: DataFlowProps) {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const cv = ref.current
    if (!cv) return
    const ctx = cv.getContext('2d')!
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    let w = 0, h = 0, raf = 0, t = 0
    const rgb = hex(tint)

    function resize() {
      const r = cv!.getBoundingClientRect()
      w = r.width; h = r.height
      cv!.width = w * dpr; cv!.height = h * dpr
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    }
    function pathY(x: number, i: number) {
      const base = ((i + 1) / (lines + 1)) * h
      return base + Math.sin(x / 90 + i * 1.3 + t / 60) * (h / (lines + 3))
    }
    function frame() {
      ctx.clearRect(0, 0, w, h)
      for (let i = 0; i < lines; i++) {
        ctx.beginPath()
        for (let x = 0; x <= w; x += 6) ctx.lineTo(x, pathY(x, i))
        ctx.strokeStyle = `rgba(${rgb},0.10)`
        ctx.lineWidth = 1
        ctx.stroke()
        for (let p = 0; p < perLine; p++) {
          const prog = ((t * 0.5 * speed + (p / perLine) * w + i * 40) % w)
          const py = pathY(prog, i)
          const g = ctx.createRadialGradient(prog, py, 0, prog, py, 9)
          g.addColorStop(0, `rgba(${rgb},0.9)`)
          g.addColorStop(1, `rgba(${rgb},0)`)
          ctx.fillStyle = g
          ctx.beginPath(); ctx.arc(prog, py, 9, 0, Math.PI * 2); ctx.fill()
        }
      }
      t += reduced ? 0 : 1
      raf = reduced ? 0 : requestAnimationFrame(frame)
    }
    resize(); frame()
    window.addEventListener('resize', resize)
    return () => { cancelAnimationFrame(raf); window.removeEventListener('resize', resize) }
  }, [lines, perLine, tint, speed])
  return <canvas ref={ref} className={className} aria-hidden style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none', ...style }} />
}

function hex(h: string) {
  const m = h.replace('#', '')
  const n = m.length === 3 ? m.split('').map((c) => c + c).join('') : m
  return `${parseInt(n.slice(0, 2), 16)},${parseInt(n.slice(2, 4), 16)},${parseInt(n.slice(4, 6), 16)}`
}

export default DataFlow
