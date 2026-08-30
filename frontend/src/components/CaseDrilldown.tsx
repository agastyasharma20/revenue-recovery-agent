import { useEffect, useState } from 'react'
import { api } from '../api'
import type { CaseSummary, CaseDetail, AuditVerifyResult, VoiceScript } from '../types'

export function CaseDrilldown({ runId }: { runId: string }) {
  const [cases, setCases] = useState<CaseSummary[]>([])
  const [selected, setSelected] = useState<string>('')
  const [detail, setDetail] = useState<CaseDetail | null>(null)
  const [verify, setVerify] = useState<AuditVerifyResult | null>(null)
  const [script, setScript] = useState<VoiceScript | null>(null)
  const [scriptLoading, setScriptLoading] = useState(false)

  useEffect(() => {
    // Clear `selected` (not just `detail`) the moment runId changes -- the
    // effect below fires on [runId, selected], and without this, one
    // render slips through where runId is already the NEW run but
    // `selected` still holds the PREVIOUS run's event_id, firing a
    // guaranteed-404 getCaseDetail(newRunId, oldEventId) request. Caught
    // via a live 404 in the network log while testing "generate new batch"
    // in the actual browser, not by inspection alone.
    setCases([])
    setSelected('')
    setDetail(null)
    setScript(null)
    setVerify(null)
    api.getCases(runId, true, 200).then((c) => {
      setCases(c)
      if (c.length > 0) setSelected(c[0].event_id)
    })
    api.verifyAudit(runId).then(setVerify)
  }, [runId])

  useEffect(() => {
    if (!selected) return
    setScript(null)
    api.getCaseDetail(runId, selected).then(setDetail)
  }, [runId, selected])

  const loadScript = async () => {
    setScriptLoading(true)
    try {
      setScript(await api.getVoiceScript(runId, selected))
    } finally {
      setScriptLoading(false)
    }
  }

  return (
    <div className="card">
      <div className="section-title">
        <span className="emoji">🔍</span>
        <span>Per-case audit drill-down</span>
      </div>

      {verify && (
        <div className={`banner ${verify.ok ? 'banner-success' : 'banner-danger'}`} style={{ marginBottom: 14 }}>
          {verify.ok ? '✅' : '⚠️'} {verify.detail}
        </div>
      )}

      {cases.length === 0 ? (
        <p className="loading-text">No pursued cases in this batch to drill into.</p>
      ) : (
        <>
          <select className="select-box" value={selected} onChange={(e) => setSelected(e.target.value)}>
            {cases.map((c) => (
              <option key={c.event_id} value={c.event_id}>
                {c.event_id.slice(0, 8)}… · {c.source} · {c.decline_reason} · ₹{c.amount.toLocaleString('en-IN')}
              </option>
            ))}
          </select>

          {detail && (
            <div className="two-col" style={{ marginTop: 16 }}>
              <div>
                <h4 style={{ fontSize: 13, marginBottom: 8 }}>Event & Diagnosis</h4>
                <table className="kv-table">
                  <tbody>
                    <tr><td>Source</td><td>{detail.payload.source}</td></tr>
                    <tr><td>Decline reason</td><td>{detail.payload.decline_reason}</td></tr>
                    <tr><td>Amount</td><td>₹{detail.payload.amount.toLocaleString('en-IN')}</td></tr>
                    <tr><td>Customer segment</td><td>{detail.payload.customer_segment}</td></tr>
                    <tr><td>Diagnosis</td><td><span className="badge badge-brand">{detail.payload.diagnosis.category}</span></td></tr>
                    <tr><td>Confidence</td><td>{(detail.payload.diagnosis.confidence * 100).toFixed(0)}% {detail.payload.diagnosis.llm_used && <span className="badge badge-muted">LLM-refined</span>}</td></tr>
                    <tr><td>Rationale</td><td style={{ fontSize: 12 }}>{detail.payload.diagnosis.rationale}</td></tr>
                  </tbody>
                </table>

                <h4 style={{ fontSize: 13, margin: '14px 0 8px' }}>Prioritization (EV)</h4>
                <table className="kv-table">
                  <tbody>
                    <tr><td>EV</td><td>₹{detail.payload.priority.ev.toLocaleString('en-IN', { maximumFractionDigits: 2 })}</td></tr>
                    <tr><td>Pursue?</td><td>{detail.payload.priority.pursue ? <span className="badge badge-success">yes</span> : <span className="badge badge-danger">no</span>}</td></tr>
                    <tr><td>Reason</td><td style={{ fontSize: 12 }}>{detail.payload.priority.reason}</td></tr>
                  </tbody>
                </table>
              </div>

              <div>
                <h4 style={{ fontSize: 13, marginBottom: 8 }}>Compliance & Action</h4>
                <table className="kv-table">
                  <tbody>
                    <tr><td>Compliance</td><td>{detail.payload.compliance.allowed ? <span className="badge badge-success">allowed</span> : <span className="badge badge-danger">blocked</span>} (rules v{detail.payload.compliance.rules_version})</td></tr>
                    <tr><td>Reason</td><td style={{ fontSize: 12 }}>{detail.payload.compliance.reason}</td></tr>
                    <tr><td>Chosen action</td><td><span className="badge badge-brand">{detail.payload.chosen_action}</span></td></tr>
                    <tr><td>Outcome</td><td>
                      {detail.payload.outcome
                        ? (detail.payload.outcome.recovered
                          ? <span className="badge badge-success">recovered ₹{detail.payload.outcome.amount_recovered.toLocaleString('en-IN')}</span>
                          : <span className="badge badge-danger">not recovered</span>)
                        : <span className="badge badge-muted">n/a</span>}
                    </td></tr>
                    {detail.payload.promise && (
                      <tr><td>Promise-to-pay</td><td>
                        <span className={`badge ${detail.payload.promise.status === 'kept' ? 'badge-success' : 'badge-danger'}`}>
                          {detail.payload.promise.status}
                        </span> due {new Date(detail.payload.promise.promised_date).toLocaleDateString()}
                      </td></tr>
                    )}
                  </tbody>
                </table>

                <h4 style={{ fontSize: 13, margin: '14px 0 8px' }}>Tamper-evidence (hash chain)</h4>
                {detail.audit_chain ? (
                  <>
                    <div className="mono-hash">prev: {detail.audit_chain.prev_hash}</div>
                    <div className="mono-hash">this: {detail.audit_chain.this_hash}</div>
                  </>
                ) : (
                  <p className="loading-text">Not found in audit log.</p>
                )}
              </div>
            </div>
          )}

          {detail && (
            <div style={{ marginTop: 18 }}>
              <button className="btn btn-secondary" onClick={loadScript} disabled={scriptLoading}>
                {scriptLoading ? '🎙️ Generating…' : '🎙️ Generate Hinglish voice-recovery script'}
              </button>
              {script && (
                <div style={{ marginTop: 12 }}>
                  <div style={{ marginBottom: 8 }}>
                    <span className={`badge ${script.generated_by === 'llm' ? 'badge-success' : 'badge-muted'}`}>
                      {script.generated_by === 'llm' ? 'live LLM generation' : 'template fallback'}
                    </span>
                  </div>
                  <div className="script-line"><span className="tag">Opening</span>{script.opening_line}</div>
                  <div className="script-line"><span className="tag">Main ask</span>{script.main_ask}</div>
                  {Object.entries(script.objection_handling).map(([objection, response]) => (
                    <div className="script-line" key={objection}>
                      <span className="tag">If customer says: "{objection}"</span>{response}
                    </div>
                  ))}
                  <div className="script-line"><span className="tag">Closing</span>{script.closing_line}</div>
                  <p style={{ fontSize: 11, color: 'var(--text-faint)' }}>
                    This is a generated call script for a human agent — not a live phone call or voice bot.
                  </p>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}
