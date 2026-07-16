import React from 'react'
import { color, font, radius } from '../tokens'

export interface StatCardProps {
  value: React.ReactNode
  label: string
  hint?: string
  /** 涨跌文本，如 "▲ 较昨日 19.8%" */
  change?: string
  changeBad?: boolean
  icon?: React.ReactNode
  iconTone?: 'gold' | 'cyan' | 'blue' | 'green' | 'red'
  accent?: boolean
}

const toneBg: Record<string, string> = {
  gold: 'rgba(231,178,76,.15)',
  cyan: 'rgba(45,212,191,.14)',
  blue: 'rgba(70,180,255,.14)',
  green: 'rgba(43,213,118,.14)',
  red: 'rgba(255,85,102,.14)',
}

/** 黄雀指标卡：图标徽章 + 大数值 + 标签 + 较昨日涨跌 */
export function StatCard({ value, label, hint, change, changeBad, icon, iconTone = 'gold', accent }: StatCardProps) {
  return (
    <div style={{ background: color.panel, border: `1px solid ${color.lineSoft}`, borderRadius: radius.lg, padding: 18, position: 'relative', overflow: 'hidden' }}>
      {icon && (
        <div style={{ width: 30, height: 30, borderRadius: 9, display: 'grid', placeItems: 'center', fontSize: 16, marginBottom: 12, background: toneBg[iconTone] }}>
          {icon}
        </div>
      )}
      <div style={{ fontFamily: font.mono, fontVariantNumeric: 'tabular-nums', fontSize: 30, fontWeight: 800, lineHeight: 1, color: accent ? color.goldSoft : color.txt }}>
        {value}
      </div>
      <div style={{ fontSize: 12.5, color: color.txtDim, marginTop: 9, fontWeight: 600 }}>{label}</div>
      {hint && <div style={{ fontSize: 11, color: color.txtFaint, marginTop: 3 }}>{hint}</div>}
      {change && (
        <div style={{ fontSize: 11, marginTop: 8, fontWeight: 600, color: changeBad ? color.red : color.green }}>{change}</div>
      )}
    </div>
  )
}

export default StatCard
