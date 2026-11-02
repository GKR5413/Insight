import type { Quote } from '../api'
import { asOf, dollarsCompact, compact, price, signed } from '../format'

export default function QuoteCard({ quote: q }: { quote: Quote }) {
  const dir = (q.change ?? 0) > 0 ? 'up' : (q.change ?? 0) < 0 ? 'down' : 'flat'
  const arrow = dir === 'up' ? '▲' : dir === 'down' ? '▼' : '■'
  const range = (lo: number | null, hi: number | null) =>
    lo != null && hi != null ? `${price(lo, q.currency)} – ${price(hi, q.currency)}` : null

  const rows: [string, string | null][] = [
    ['Open', q.open != null ? price(q.open, q.currency) : null],
    ['Previous close', q.previous_close != null ? price(q.previous_close, q.currency) : null],
    ['Day range', range(q.day_low, q.day_high)],
    ['52-week range', range(q.year_low, q.year_high)],
    ['Volume', q.volume != null ? compact(q.volume) : null],
    ['Market cap', q.market_cap != null ? dollarsCompact(q.market_cap) : null],
  ]

  return (
    <section className="card quote" aria-label={`Quote for ${q.symbol}`}>
      <div className="quote-head">
        <span className="symbol">{q.symbol}</span>
        <span className="price">{price(q.price, q.currency)}</span>
        {q.change != null && (
          <span className={`change ${dir}`}>
            <span aria-hidden="true">{arrow}</span> {signed(q.change)}
            {q.change_pct != null && ` (${signed(q.change_pct)}%)`}
          </span>
        )}
      </div>
      <dl className="kv">
        {rows
          .filter(([, v]) => v)
          .map(([k, v]) => (
            <div key={k}>
              <dt>{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
      </dl>
      <p className="fine">
        {q.delayed ? 'Delayed · ' : ''}
        {q.source}
        {q.as_of && ` · as of ${asOf(q.as_of)}`}
      </p>
    </section>
  )
}
