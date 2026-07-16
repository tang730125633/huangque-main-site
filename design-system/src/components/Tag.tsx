import React from 'react'
import { color, font, radius } from '../tokens'

export interface TagProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: 'gold' | 'cyan' | 'green' | 'blue' | 'red' | 'muted'
}

const tones: Record<string, { c: string; b: string }> = {
  gold: { c: color.goldSoft, b: 'rgba(231,178,76,.12)' },
  cyan: { c: color.cyan, b: 'rgba(45,212,191,.12)' },
  green: { c: color.green, b: 'rgba(43,213,118,.12)' },
  blue: { c: color.blue, b: 'rgba(70,180,255,.12)' },
  red: { c: color.red, b: 'rgba(255,85,102,.12)' },
  muted: { c: color.txtDim, b: color.surface },
}

/** 黄雀标签 chip（语义色） */
export function Tag({ tone = 'muted', style, children, ...rest }: TagProps) {
  const t = tones[tone]
  return (
    <span
      style={{ fontFamily: font.mono, fontSize: 10.5, fontWeight: 700, padding: '3px 8px', borderRadius: radius.sm, color: t.c, background: t.b, border: `1px solid ${t.c}33`, ...style }}
      {...rest}
    >
      {children}
    </span>
  )
}

export default Tag
