import React from 'react'
import { color, font } from '../tokens'
import { WebGLParticles } from './WebGLParticles'

export interface GlowHeroProps {
  eyebrow?: string
  title: React.ReactNode
  subtitle?: React.ReactNode
  children?: React.ReactNode
  /** 背景粒子：webgl(3D) | none */
  bg?: 'webgl' | 'none'
  height?: number | string
}

/**
 * 黄雀首屏：WebGL 粒子背景 + 径向金光 + 标题/副标题/CTA。
 */
export function GlowHero({ eyebrow, title, subtitle, children, bg = 'webgl', height = '88vh' }: GlowHeroProps) {
  return (
    <section
      style={{
        position: 'relative',
        minHeight: height,
        display: 'flex',
        alignItems: 'center',
        overflow: 'hidden',
        background: `radial-gradient(900px 520px at 72% 18%, rgba(231,178,76,.14), transparent 60%), radial-gradient(700px 500px at 12% 80%, rgba(45,212,191,.08), transparent 55%), ${color.bg}`,
      }}
    >
      {bg === 'webgl' && <WebGLParticles />}
      <div style={{ position: 'relative', zIndex: 2, maxWidth: 1180, margin: '0 auto', padding: '0 32px', width: '100%' }}>
        {eyebrow && (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, fontSize: 12, letterSpacing: 2, color: color.goldSoft, textTransform: 'uppercase', border: '1px solid rgba(231,178,76,.28)', borderRadius: 999, padding: '6px 14px', marginBottom: 22 }}>
            <i style={{ width: 6, height: 6, borderRadius: '50%', background: color.gold, boxShadow: `0 0 10px ${color.gold}`, display: 'inline-block' }} />
            {eyebrow}
          </span>
        )}
        <h1 style={{ fontFamily: font.sans, fontSize: 'clamp(34px,5vw,56px)', lineHeight: 1.12, fontWeight: 900, letterSpacing: -1, color: color.txt, margin: 0, maxWidth: 760 }}>
          {title}
        </h1>
        {subtitle && (
          <p style={{ marginTop: 22, fontSize: 17, color: color.txtDim, maxWidth: 540 }}>{subtitle}</p>
        )}
        {children && <div style={{ marginTop: 32, display: 'flex', gap: 14, flexWrap: 'wrap' }}>{children}</div>}
      </div>
    </section>
  )
}

export default GlowHero
