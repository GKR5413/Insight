import { useState } from 'react'
import type { Brief, Claim, Source } from '../api'
import { asOf } from '../format'

function SourceLink({ s }: { s: Source }) {
  return s.url ? (
    <a href={s.url} target="_blank" rel="noopener noreferrer">
      {s.label} <span aria-hidden="true">↗</span>
      <span className="sr-only"> (opens the filing on sec.gov)</span>
    </a>
  ) : (
    <span>{s.label}</span>
  )
}

function ClaimItem({ claim, sources }: { claim: Claim; sources: Map<number, Source> }) {
  const [open, setOpen] = useState<number | null>(null)
  const refs = [...new Set(claim.citations.map((c) => c.ref))] // a claim may quote one source twice; show one chip
  const quotes = claim.citations.filter((c) => c.ref === open)
  const src = open != null ? sources.get(open) : undefined
  return (
    <li className="claim">
      <p>
        {claim.text}{' '}
        {refs.map((ref) => (
          <button
            key={ref}
            type="button"
            className={`cite ${open === ref ? 'on' : ''}`}
            aria-expanded={open === ref}
            aria-label={`Source ${ref}`}
            onClick={() => setOpen(open === ref ? null : ref)}
          >
            {ref}
          </button>
        ))}
      </p>
      {src && quotes.length > 0 && (
        <blockquote className="evidence">
          {quotes.map((q, i) => (
            <p key={i} className="quote-text">
              “{q.quote}”
            </p>
          ))}
          <p className="src">
            <span className={`kind ${src.kind}`}>{src.kind === 'sheet' ? 'Figure' : 'Filing'}</span> <SourceLink s={src} />
          </p>
        </blockquote>
      )}
    </li>
  )
}

export default function BriefView({ brief, runWarnings, onRefresh }: { brief: Brief; runWarnings: string[]; onRefresh: () => void }) {
  const sources = new Map(brief.sources.map((s) => [s.ref, s]))
  const warnings = [...new Set([...brief.warnings, ...runWarnings])]
  const m = brief.meta
  const claimCount = brief.sections.reduce((n, s) => n + s.claims.length, 0)

  return (
    <article className="brief">
      <header className="brief-head">
        <div>
          <h2>
            {brief.company} <span className="muted">({brief.ticker})</span>
          </h2>
          <p className="fine">
            Generated {asOf(brief.generated_at)} · {m.cache_hit ? 'served from cache' : 'freshly built'} · model: {m.llm}
          </p>
        </div>
        <button type="button" className="ghost" onClick={onRefresh} title="Re-index filings and rebuild, ignoring the cache">
          Rebuild
        </button>
      </header>

      <p className={`verify ${brief.removed_claims ? 'some' : 'all'}`}>
        <span aria-hidden="true">{brief.removed_claims ? '⚠' : '✓'}</span>{' '}
        {claimCount} claim{claimCount === 1 ? '' : 's'} passed source verification
        {brief.removed_claims > 0 &&
          `; ${brief.removed_claims} removed because their quotes could not be found in the cited source${m.retries ? ' after one retry' : ''}`}
        .
      </p>

      {warnings.length > 0 && (
        <section className="notice warn">
          <ul>
            {warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </section>
      )}

      {brief.sections.map((sec) => (
        <section key={sec.facet} className="facet">
          <h3>{sec.title}</h3>
          <ul className="claims">
            {sec.claims.map((c, i) => (
              <ClaimItem key={i} claim={c} sources={sources} />
            ))}
          </ul>
        </section>
      ))}

      {brief.sources.length > 0 && (
        <section className="facet sources">
          <h3>Sources</h3>
          <ol>
            {brief.sources.map((s) => (
              <li key={s.ref} value={s.ref}>
                <SourceLink s={s} />
              </li>
            ))}
          </ol>
        </section>
      )}

      <p className="fine meta">
        {m.evidence_items} evidence items considered · embeddings: {m.embedding}
        {m.figure_sources?.length ? ` · figures: ${m.figure_sources.map((f) => FIG[f] ?? f).join(', ')}` : ''}
      </p>
    </article>
  )
}

const FIG: Record<string, string> = {
  fsds: 'SEC Financial Statement Data Sets',
  sec_statement: 'SEC R pages',
  xlsx_upload: 'uploaded workbook',
}
