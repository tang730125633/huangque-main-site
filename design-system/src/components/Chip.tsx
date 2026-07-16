import React from 'react'
import { color, gradient, radius } from '../tokens'

export interface ChipProps extends React.HTMLAttributes<HTMLSpanElement> {
  active?: boolean
}

/** 黄雀筛选 chip：选中态金色实心 */
export function Chip({ active, style, children, ...rest }: ChipProps) {
  return (
    <span
      style={{
        fontSize: 13,
        padding: '7px 15px',
        borderRadius: radius.pill,
        cursor: 'pointer',
        transition: '.15s',
        userSelect: 'none',
        ...(active
          ? { color: '#0a0e16', background: gradient.gold, border: '1px solid transparent', fontWeight: 700 }
          : { color: color.txtDim, background: color.surface, border: `1px solid ${color.line}` }),
        ...style,
      }}
      {...rest}
    >
      {children}
    </span>
  )
}

export default Chip
