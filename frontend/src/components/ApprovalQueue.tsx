import { useEffect, useState } from 'react'
import { api } from '../api'
import type { CaseSummary } from '../types'

export function ApprovalQueue({ runId }: { runId: string }) {
  const [pending, setPending] = useState<CaseSummary[] | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const refresh = () => api.getPendingApprovals(runId).then(setPending)

  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId])

  if (pending === null) return null
  if (pending.length === 0) return null // HITL mode off, or nothing needs sign-off in this batch

  const act = async (fn: () => Promise<CaseSummary>, eventId: string) => {
    setBusy(eventId)
    try {
      await fn()
      await refresh()
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="card">
      <div className="section-title">
        <span className="emoji">🧑‍⚖️</span>
        <span>Human-in-the-loop approval queue</span>
      </div>
      <p style={{ fontSize: 12.5, color: 'var(--text-muted)', marginBottom: 14 }}>
        "AI proposes, a human authorizes." These {pending.length} actions were selected by the agent but
        exceed an auto-approval threshold (large collections escalations, large discounts) or are flagged
        risk-block (suspected fraud) — they wait here until approved or rejected instead of executing
        unattended.
      </p>
      <div style={{ overflowX: 'auto' }}>
        <table className="table">
          <thead>
            <tr><th>Source</th><th>Reason</th><th>Amount</th><th>Proposed action</th><th>Why it needs approval</th><th></th></tr>
          </thead>
          <tbody>
            {pending.map((c) => (
              <tr key={c.event_id}>
                <td>{c.source}</td>
                <td>{c.decline_reason}</td>
                <td>₹{c.amount.toLocaleString('en-IN')}</td>
                <td><span className="badge badge-brand">{c.chosen_action}</span></td>
                <td style={{ fontSize: 11.5, maxWidth: 280 }}>{c.approval_reason}</td>
                <td style={{ whiteSpace: 'nowrap' }}>
                  <button
                    className="btn btn-primary" style={{ padding: '5px 10px', fontSize: 11.5, marginRight: 6 }}
                    disabled={busy === c.event_id}
                    onClick={() => act(() => api.approveCase(runId, c.event_id), c.event_id)}
                  >
                    ✓ Approve
                  </button>
                  <button
                    className="btn btn-secondary" style={{ padding: '5px 10px', fontSize: 11.5 }}
                    disabled={busy === c.event_id}
                    onClick={() => act(() => api.rejectCase(runId, c.event_id, 'rejected by reviewer'), c.event_id)}
                  >
                    ✗ Reject
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
