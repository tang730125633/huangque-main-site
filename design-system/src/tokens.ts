/**
 * 黄雀 AI · 设计 token
 * 暖白运营台 + 墨绿主色。与根目录 DESIGN.md 同源。
 * Claude Design /design-sync 读取这里的 token。
 */
export const color = {
  bg: '#F6F7F2',
  panel: '#FFFFFF',
  panel2: '#EEF2EC',
  surface: '#FFFFFF',
  surface2: '#EEF2EC',
  line: '#D9DED6',
  lineSoft: '#E8EBE5',
  lineStrong: '#C8D0C8',
  txt: '#191C1A',
  txtDim: '#68736D',
  txtFaint: '#96A099',
  gold: '#B86B2B',
  goldSoft: '#CF8342',
  cyan: '#176B5B',
  blue: '#2F6FED',
  green: '#16803C',
  warn: '#B7791F',
  red: '#C2413A',
} as const

export const font = {
  sans: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif',
  mono: 'ui-monospace, "SF Mono", Menlo, "JetBrains Mono", Consolas, monospace',
} as const

export const radius = { sm: 4, md: 6, lg: 8, xl: 10, xxl: 12, pill: 999 } as const

export const space = { xs: 6, sm: 10, md: 14, lg: 18, xl: 24, xxl: 32 } as const

export const shadow = {
  card: '0 14px 36px rgba(0,0,0,.45)',
  gold: '0 8px 24px rgba(231,178,76,.26)',
  pop: '0 16px 44px rgba(0,0,0,.5)',
} as const

export const gradient = {
  gold: `linear-gradient(135deg, ${color.goldSoft}, ${color.gold})`,
  goldGlow: 'linear-gradient(120deg, rgba(231,178,76,.16), rgba(45,212,191,.07))',
} as const

export const tokens = { color, font, radius, space, shadow, gradient } as const
export type Tokens = typeof tokens
