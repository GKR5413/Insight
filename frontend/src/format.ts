const num = (d = 2) => new Intl.NumberFormat('en-US', { minimumFractionDigits: d, maximumFractionDigits: d })

export const price = (v: number, currency = 'USD') =>
  new Intl.NumberFormat('en-US', { style: 'currency', currency }).format(v)

export const signed = (v: number, d = 2) => `${v > 0 ? '+' : v < 0 ? '−' : ''}${num(d).format(Math.abs(v))}`

export const compact = (v: number) =>
  new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 2 }).format(v)

export const dollarsCompact = (v: number) => `$${compact(v)}`

export function asOf(s: string): string {
  if (!s) return ''
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}
