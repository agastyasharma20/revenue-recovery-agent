export interface MetricsSummary {
  total_events: number
  events_pursued: number
  events_recovered: number
  total_recovered_inr: number
  recovery_rate_of_pursued: number
  recovery_rate_of_total: number
  avg_latency_ms_per_layer: Record<string, number>
  llm_fallback_rate: number
  llm_used_rate: number
  llm_error_count: number
}

export interface Incident {
  decline_reason: string
  amount_bucket: string
  count: number
  message: string
}

export interface CreateRunResponse {
  run_id: string
  summary: MetricsSummary
  incidents: Incident[]
}

export interface CaseSummary {
  event_id: string
  source: string
  decline_reason: string
  amount: number
  customer_segment: string
  customer_id: string
  chosen_action: string
  pursued: boolean
  recovered: boolean | null
  recovered_amount: number
  ev: number
  requires_approval?: boolean
  approval_status?: string
  approval_reason?: string
  diagnosis_category: string
}

export interface CaseDetail {
  case: CaseSummary
  payload: {
    event_id: string
    trace_id: string
    customer_id: string
    source: string
    decline_reason: string
    amount: number
    retry_count: number
    customer_segment: string
    diagnosis: { category: string; confidence: number; rationale: string; llm_used: boolean }
    priority: { ev: number; pursue: boolean; reason: string }
    compliance: { allowed: boolean; reason: string; rules_version: number }
    chosen_action: string
    outcome: { recovered: boolean; probability_used: number; amount_recovered: number } | null
    promise: { promise_id: string; status: string; promised_date: string; amount: number } | null
    timeline: { stage: string; at: string; note: string }[]
  }
  audit_chain: { prev_hash: string; this_hash: string; logged_at: string } | null
}

export interface AuditVerifyResult {
  ok: boolean
  total_records: number
  first_bad_index: number | null
  detail: string
}

export interface PortfolioResult {
  cases_available: number
  capacity_hours?: number
  topn?: { selected: number; value: number; hours_used: number }
  knapsack?: { selected: number; value: number; hours_used: number }
  value_gain?: number
  value_gain_pct?: number
}

export interface PromiseHistoryEntry {
  event_id: string
  amount: number
  created_at: string
  status: string
  promised_date: string
  ev: number
  reliability_adjustment_applied: boolean
}

export interface PromisesResult {
  summary: {
    total_promises: number
    kept: number
    broken: number
    kept_rate: number
    distinct_customers: number
    total_inr_promised: number
    total_inr_kept: number
  }
  repeat_customers: Record<string, PromiseHistoryEntry[]>
}

export interface VoiceScript {
  event_id: string
  language: string
  opening_line: string
  main_ask: string
  objection_handling: Record<string, string>
  closing_line: string
  generated_by: 'llm' | 'template_fallback'
  llm_error: string | null
}

export interface BanditConvergenceResult {
  window_rates: number[]
  window_size: number
  rounds: number
  learned_mapping: { decline_reason: string; bandit_learned: string; oracle_best: string; match: boolean }[]
  match_count: number
  total_segments: number
}

export interface LiveTick {
  type: 'tick' | 'done' | 'error'
  index?: number
  total?: number
  event_id?: string
  source?: string
  decline_reason?: string
  chosen_action?: string
  pursued?: boolean
  recovered?: boolean
  recovered_amount?: number
  running_total?: number
  running_recovered?: number
  message?: string
}
