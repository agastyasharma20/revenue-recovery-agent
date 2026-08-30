import { useRef, useState } from 'react'
import { api } from '../api'
import type { MetricsSummary, LiveTick } from '../types'

interface Props {
  runId: string
  summary: MetricsSummary
}

const inr = (n: number) => `₹${n.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`

export function LiveCounter({ runId, summary }: Props) {
  const [replaying, setReplaying] = useState(false)
  const [liveTotal, setLiveTotal] = useState<number | null>(null)
  const [progress, setProgress] = useState(0)
  const wsRef = useRef<WebSocket | null>(null)

  const startReplay = () => {
    wsRef.current?.close()
    setReplaying(true)
    setLiveTotal(0)
    setProgress(0)

    const ws = new WebSocket(api.liveSocketUrl(runId, 8))
    wsRef.current = ws
    ws.onmessage = (msg) => {
      const data: LiveTick = JSON.parse(msg.data)
      if (data.type === 'tick') {
        setLiveTotal(data.running_total ?? 0)
        setProgress(((data.index ?? 0) + 1) / (data.total ?? 1))
      } else if (data.type === 'done') {
        setLiveTotal(data.running_total ?? 0)
        setProgress(1)
        setReplaying(false)
      }
    }
    ws.onerror = () => setReplaying(false)
  }

  const displayedTotal = liveTotal ?? summary.total_recovered_inr

  return (
    <div className="card">
      <div className="section-title" style={{ marginBottom: 14 }}>
        <span className="emoji">📊</span>
        <span>Live recovery counter</span>
      </div>
      <div className="stat-grid">
        <div className="stat-card">
          <div className="stat-label">Total INR recovered</div>
          <div className="stat-value mono">{inr(displayedTotal)}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Events processed</div>
          <div className="stat-value mono">{summary.total_events}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Events pursued</div>
          <div className="stat-value mono">{summary.events_pursued}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Recovery rate (of pursued)</div>
          <div className="stat-value mono">{(summary.recovery_rate_of_pursued * 100).toFixed(1)}%</div>
        </div>
      </div>

      <div style={{ marginTop: 16 }}>
        <button className="btn btn-secondary" onClick={startReplay} disabled={replaying}>
          {replaying ? '▶ Replaying…' : '↻ Replay live (WebSocket)'}
        </button>
        {(replaying || progress > 0) && (
          <div style={{ marginTop: 10 }}>
            <div className="progress-track">
              <div className="progress-fill" style={{ width: `${progress * 100}%` }} />
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
