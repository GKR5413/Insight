import { STAGES, type StageState } from '../useBrief'

export default function Progress({ stages, ticker }: { stages: Record<string, StageState>; ticker: string }) {
  return (
    <section className="card" aria-live="polite" aria-label={`Building brief for ${ticker}`}>
      <h2 className="h-small">Building the brief for {ticker}</h2>
      <ol className="steps">
        {STAGES.map(({ key, label }) => {
          const s = stages[key]
          return (
            <li key={key} className={s.status}>
              <span className="mark" aria-hidden="true">
                {s.status === 'done' ? '✓' : s.status === 'active' ? <span className="spin" /> : '○'}
              </span>
              <span className="step-label">{label}</span>
              {s.message && <span className="step-msg">{s.message}</span>}
            </li>
          )
        })}
      </ol>
      <p className="fine">First lookups index the filings (a minute or so). Repeat lookups are instant.</p>
    </section>
  )
}
