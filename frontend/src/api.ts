export interface Quote {
  symbol: string
  price: number
  change: number | null
  change_pct: number | null
  previous_close: number | null
  open: number | null
  day_high: number | null
  day_low: number | null
  year_high: number | null
  year_low: number | null
  volume: number | null
  market_cap: number | null
  currency: string
  as_of: string
  source: string
  delayed: boolean
}

export interface Source {
  ref: number
  kind: 'doc' | 'sheet'
  label: string
  url: string | null
  form: string
  item: string
  filed: string | null
  accession: string
  file: string | null
  sheet: string | null
  row: number | null
}

export interface Claim {
  text: string
  citations: { ref: number; quote: string }[]
}

export interface Section {
  facet: string
  title: string
  claims: Claim[]
}

export interface Brief {
  ticker: string
  company: string
  generated_at: string
  filings: { form: string; filed: string; accession: string; url: string }[]
  sections: Section[]
  sources: Source[]
  removed_claims: number
  warnings: string[]
  meta: {
    llm: string
    embedding: string
    first_pass_failed: number
    retries: number
    evidence_items: number
    cache_hit: boolean
    figure_sources?: string[]
  }
}

export interface Health {
  ok: boolean
  problems: string[]
  config: { llm: string; embeddings: string; milvus: string; quote_providers: string; langsmith_tracing: boolean }
  cache_backend?: string
  evidence_rows?: number
  fsds?: { available: boolean; filings?: number; filed_from?: string; filed_to?: string; quarters?: string[] }
}

export type StageStatus = 'start' | 'done' | 'info'

export interface ProgressEvent {
  type: 'progress'
  stage: string
  status: StageStatus
  message: string
}

export async function fetchHealth(): Promise<Health> {
  const r = await fetch('/api/health')
  if (!r.ok) throw new Error(`Health check failed (${r.status})`)
  return r.json()
}
