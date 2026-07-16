import React from 'react'
import { color, font, radius, gradient, shadow } from '../tokens'

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'gold' | 'ghost' | 'soft'
  size?: 'sm' | 'md' | 'lg'
}

/** 黄雀按钮：gold(金色实心 CTA) / ghost(描边) / soft(低调) */
export function Button({ variant = 'gold', size = 'md', style, children, ...rest }: ButtonProps) {
  const pad = size === 'lg' ? '13px 26px' : size === 'sm' ? '8px 15px' : '11px 20px'
  const fs = size === 'lg' ? 15 : size === 'sm' ? 13 : 14.5
  const base: React.CSSProperties = {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 8,
    fontFamily: font.sans,
    fontWeight: 800,
    fontSize: fs,
    padding: pad,
    borderRadius: radius.md,
    border: 'none',
    cursor: 'pointer',
    transition: '.16s',
    whiteSpace: 'nowrap',
  }
  const variants: Record<string, React.CSSProperties> = {
    gold: { background: gradient.gold, color: '#0a0e16', boxShadow: shadow.gold },
    ghost: { background: 'rgba(255,255,255,.02)', color: color.txt, border: `1px solid ${color.lineStrong}` },
    soft: { background: color.surface, color: color.txt, border: `1px solid ${color.line}` },
  }
  return (
    <button style={{ ...base, ...variants[variant], ...style }} {...rest}>
      {children}
    </button>
  )
}

export default Button
