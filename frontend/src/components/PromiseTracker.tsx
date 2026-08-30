import { useEffect, useState } from 'react'
import { api } from '../api'
import type { PromisesResult } from '../types'

export function PromiseTracker({ runId }: { runId: string }) {
  const [data, setData] = useState<PromisesResult | null>(null)

  useEffect(() => {
    api.getPromises(runId).then(setData)
  }, [runId])

  if (!data) return null
  const { summary, repeat_customers } = data

  if (summary.total_promises === 0) {
    return (
      <div className="card">
        <div className="section-title"><span className="emoji">🤝</span><span>Promise-to-pay tracking</span></div>
        <p className="loading-text">No collections/human-call escalations in this batch made a promise-to-pay.</p>
      </div>
    )
  }

  const repeatEntries = Object.entries(repeat_customers).slice(0, 5)

  return (
    <div className="card">
      <div className="section-title">
        <span className="emoji">🤝</span>
        <span>Promise-to-pay tracking</span>
      </div>
      <p style={{ fontSize: 12.5, color: 'var(--text-muted)', marginBottom: 14 }}>
        Named explicitly in the buildathon brief alongside B2B receivables chasing. A broken promise
        measurably lowers EV on that customer's <em>next</em> invoice (±50% based on their track record) —
        a real cross-event feedback loop, not a per-event decision made in isolation.
      </p>

      <div className="stat-grid">
        <div className="stat-card">
          <div className="stat-label">Promises made</div>
          <div className="stat-value mono" style={{ fontSize: 20 }}>{summary.total_promises}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Kept rate</div>
          <div className="stat-value mono" style={{ fontSize: 20 }}>{(summary.kept_rate * 100).toFixed(1)}%</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Distinct customers</div>
          <div className="stat-value mono" style={{ fontSize: 20 }}>{summary.distinct_customers}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">INR kept / promised</div>
          <div className="stat-value mono" style={{ fontSize: 16 }}>
            ₹{(summary.total_inr_kept / 1000).toFixed(0)}k / ₹{(summary.total_inr_promised / 1000).toFixed(0)}k
          </div>
        </div>
      </div>

      {repeatEntries.length > 0 && (
        <>
          <h4 style={{ fontSize: 13, margin: '18px 0 8px' }}>Repeat customers (reliability feedback in action)</h4>
          <div style={{ overflowX: 'auto' }}>
            <table className="table">
              <thead>
                <tr><th>Customer</th><th>Invoice date</th><th>Amount</th><th>EV</th><th>Promise</th><th>Reliability applied?</th></tr>
              </thead>
              <tbody>
                {repeatEntries.map(([cid, history]) =>
                  history
                    .slice()
                    .sort((a, b) => a.created_at.localeCompare(b.created_at))
                    .map((h, i) => (
                      <tr key={h.event_id}>
                        <td>{i === 0 ? cid : ''}</td>
                        <td>{new Date(h.created_at).toLocaleDateString()}</td>
                        <td>₹{h.amount.toLocaleString('en-IN')}</td>
                        <td>₹{h.ev.toLocaleString('en-IN', { maximumFractionDigits: 0 })}</td>
                        <td>
                          <span className={`badge ${h.status === 'kept' ? 'badge-success' : 'badge-danger'}`}>
                            {h.status}
                          </span>
                        </td>
                        <td>{h.reliability_adjustment_applied ? '✓' : '—'}</td>
                      </tr>
                    )),
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
