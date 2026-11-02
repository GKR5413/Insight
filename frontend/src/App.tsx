import { useEffect, useState, type FormEvent } from 'react'
import { fetchHealth, type Health } from './api'
import { useBrief } from './useBrief'
import QuoteCard from './components/QuoteCard'
import Progress from './components/Progress'
import BriefView from './components/BriefView'

const EXAMPLES = ['AAPL', 'MSFT', 'JPM', 'TSLA']

export default function App() {
  const [ticker, setTicker] = useState('')
  const [health, setHealth] = useState<Health | null>(null)
  const brief = useBrief()

  useEffect(() => {
    fetchHealth().then(setHealth).catch(() => setHealth(null))
  }, [])

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (ticker.trim()) brief.run(ticker)
  }
  const busy = brief.phase === 'loading'

  return (
    <main className="page">
      <header className="masthead">
        <h1>Stock Briefing</h1>
        <p>Delayed quote and an evidence-cited summary of the latest SEC filings. Every claim links to its source.</p>
      </header>

      <form className="search" onSubmit={submit} role="search">
        <label htmlFor="ticker" className="sr-only">
          Ticker symbol
        </label>
        <input
          id="ticker"
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          placeholder="Ticker, e.g. AAPL"
          autoComplete="off"
          autoCapitalize="characters"
          spellCheck={false}
          maxLength={10}
        />
        <button type="submit" disabled={busy || !ticker.trim()}>
          {busy ? 'Working…' : 'Brief me'}
        </button>
      </form>
      <div className="examples" aria-label="Examples">
        {EXAMPLES.map((t) => (
          <button
            key={t}
            type="button"
            className="chip"
            disabled={busy}
            onClick={() => {
              setTicker(t)
              brief.run(t)
            }}
          >
            {t}
          </button>
        ))}
      </div>

      {health && !health.ok && (
        <section className="notice warn" role="alert">
          <strong>Setup needed</strong>
          <ul>
            {health.problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
          <p>
            Edit <code>.env</code> (see <code>.env.example</code>) and restart the API.
          </p>
        </section>
      )}

      {brief.phase !== 'idle' && brief.quote && <QuoteCard quote={brief.quote} />}
      {busy && <Progress stages={brief.stages} ticker={brief.ticker} />}

      {brief.phase === 'error' && (
        <section className="notice error" role="alert">
          <strong>Couldn’t build the brief for {brief.ticker}</strong>
          <p>{brief.error}</p>
        </section>
      )}

      {brief.phase === 'done' && brief.brief && (
        <BriefView brief={brief.brief} runWarnings={brief.runWarnings} onRefresh={() => brief.run(brief.ticker, true)} />
      )}

      <footer className="foot">
        For advisor use only. Not investment advice. Quotes are delayed and unofficial; verify against the linked filings.
        {health?.fsds?.available && ' Statement figures: SEC Financial Statement Data Sets where loaded, SEC R pages otherwise.'}
      </footer>
    </main>
  )
}
