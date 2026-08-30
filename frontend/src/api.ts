import type {
  CreateRunResponse, CaseSummary, CaseDetail, AuditVerifyResult,
  PortfolioResult, PromisesResult, VoiceScript, BanditConvergenceResult,
} from './types'

// Same-origin in dev (proxied by vite.config.ts) and in a production build
// served behind the same host as the API -- see README for deployment notes.
const BASE = '/api'

async function getJSON<T>(path: string): Promise<T> {
  const resp = await fetch(`${BASE}${path}`)
  if (!resp.ok) throw new Error(`GET ${path} -> ${resp.status}`)
  return resp.json()
}

async function postJSON<T>(path: string, body?: unknown): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!resp.ok) {
    const text = await resp.text()
    throw new Error(`POST ${path} -> ${resp.status}: ${text}`)
  }
  return resp.json()
}

export interface CreateRunParams {
  n: number
  seed: number
  policy_mode: 'deterministic' | 'bandit'
  inject_spike: boolean
  use_llm: boolean
  auto_approve: boolean
}

export const api = {
  createRun: (params: CreateRunParams) => postJSON<CreateRunResponse>('/runs', params),
  getCases: (runId: string, pursuedOnly: boolean, limit = 300) =>
    getJSON<CaseSummary[]>(`/runs/${runId}/cases?pursued_only=${pursuedOnly}&limit=${limit}`),
  getCaseDetail: (runId: string, eventId: string) =>
    getJSON<CaseDetail>(`/runs/${runId}/cases/${eventId}`),
  verifyAudit: (runId: string) => getJSON<AuditVerifyResult>(`/runs/${runId}/audit/verify`),
  getPortfolio: (runId: string, capacityHours: number) =>
    getJSON<PortfolioResult>(`/runs/${runId}/portfolio?capacity_hours=${capacityHours}`),
  getPromises: (runId: string) => getJSON<PromisesResult>(`/runs/${runId}/promises`),
  getVoiceScript: (runId: string, eventId: string) =>
    postJSON<VoiceScript>(`/runs/${runId}/cases/${eventId}/voice-script`),
  getPendingApprovals: (runId: string) => getJSON<CaseSummary[]>(`/runs/${runId}/pending-approvals`),
  approveCase: (runId: string, eventId: string) =>
    postJSON<CaseSummary>(`/runs/${runId}/cases/${eventId}/approve`),
  rejectCase: (runId: string, eventId: string, reason: string) =>
    postJSON<CaseSummary>(`/runs/${runId}/cases/${eventId}/reject?reason=${encodeURIComponent(reason)}`),
  getBanditConvergence: (seed: number, rounds: number, window: number) =>
    getJSON<BanditConvergenceResult>(`/bandit-convergence?seed=${seed}&rounds=${rounds}&window=${window}`),
  liveSocketUrl: (runId: string, speedMs: number) => {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    return `${proto}://${window.location.host}/ws/runs/${runId}/live?speed_ms=${speedMs}`
  },
}
