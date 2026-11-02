import { useCallback, useEffect, useRef, useState } from 'react'
import type { Brief, ProgressEvent, Quote } from './api'

export const STAGES: { key: string; label: string }[] = [
  { key: 'resolve', label: 'Find company & filings' },
  { key: 'quote', label: 'Fetch delayed quote' },
  { key: 'ingest', label: 'Index new filings' },
  { key: 'retrieve', label: 'Retrieve evidence' },
  { key: 'summarize', label: 'Draft cited claims' },
  { key: 'verify', label: 'Verify citations' },
  { key: 'finalize', label: 'Assemble brief' },
]

export interface StageState {
  status: 'pending' | 'active' | 'done'
  message: string
}

type Phase = 'idle' | 'loading' | 'done' | 'error'

interface State {
  phase: Phase
  ticker: string
  stages: Record<string, StageState>
  quote: Quote | null
  brief: Brief | null
  runWarnings: string[]
  error: string | null
}

const fresh = (ticker = ''): State => ({
  phase: ticker ? 'loading' : 'idle',
  ticker,
  stages: Object.fromEntries(STAGES.map((s) => [s.key, { status: 'pending', message: '' } as StageState])),
  quote: null,
  brief: null,
  runWarnings: [],
  error: null,
})

/** Streams a brief over Server-Sent Events: progress -> quote -> result (or error). */
export function useBrief() {
  const [state, setState] = useState<State>(fresh())
  const source = useRef<EventSource | null>(null)

  const close = useCallback(() => {
    source.current?.close()
    source.current = null
  }, [])

  useEffect(() => close, [close])

  const run = useCallback(
    (ticker: string, refresh = false) => {
      close()
      const t = ticker.trim().toUpperCase()
      setState(fresh(t))
      const es = new EventSource(`/api/brief/stream?ticker=${encodeURIComponent(t)}${refresh ? '&refresh=true' : ''}`)
      source.current = es
      let finished = false

      es.addEventListener('progress', (e) => {
        const p = JSON.parse((e as MessageEvent).data) as ProgressEvent
        setState((s) => {
          const prev = s.stages[p.stage]
          if (!prev) return s
          const status = p.status === 'done' ? 'done' : 'active'
          return { ...s, stages: { ...s.stages, [p.stage]: { status, message: p.message || prev.message } } }
        })
      })
      es.addEventListener('quote', (e) => {
        const { quote } = JSON.parse((e as MessageEvent).data) as { quote: Quote }
        setState((s) => ({ ...s, quote }))
      })
      es.addEventListener('result', (e) => {
        finished = true
        const r = JSON.parse((e as MessageEvent).data) as { quote: Quote | null; brief: Brief; run_warnings: string[] }
        setState((s) => ({ ...s, phase: 'done', quote: r.quote ?? s.quote, brief: r.brief, runWarnings: r.run_warnings }))
        close()
      })
      es.addEventListener('error', (e) => {
        // Server-sent "error" events carry data; a dropped connection has none.
        const data = (e as MessageEvent).data
        if (finished) return
        finished = true
        const message = data ? (JSON.parse(data) as { message: string }).message : 'Lost connection to the server.'
        setState((s) => ({ ...s, phase: 'error', error: message }))
        close()
      })
    },
    [close],
  )

  return { ...state, run }
}
