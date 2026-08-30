import { useEffect, useState } from 'react'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import { api } from '../api'
import type { BanditConvergenceResult } from '../types'

export function BanditChart() {
  const [data, setData] = useState<BanditConvergenceResult | null>(null)
  const [loading, setLoading] = useState(false)

  const run = () => {
    setLoading(true)
    api.getBanditConvergence(7, 6000, 300).then((d) => {
      setData(d)
      setLoading(false)
    })
  }

  useEffect(run, [])

  const chartData = data?.window_rates.map((rate, i) => ({
    round: (i + 1) * (data.window_size),
    rate: +(rate * 100).toFixed(1),
  })) ?? []

  return (
    <div className="card">
      <div className="section-title">
        <span className="emoji">🎰</span>
        <span>Contextual bandit (LinUCB) convergence</span>
      </div>
      <p style={{ fontSize: 12.5, color: 'var(--text-muted)', marginBottom: 14 }}>
        One bandit's state carried forward across sequential rounds, with NO reason→action mapping
        hardcoded — it has to work out the best action per decline reason purely from outcome feedback.
      </p>

      {loading || !data ? (
        <p className="loading-text">Running {loading ? '' : ''}convergence…</p>
      ) : (
        <>
          <ResponsiveContainer width="100%" height={220}>
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
              <XAxis dataKey="round" tick={{ fontSize: 11 }} label={{ value: 'Rounds processed', position: 'insideBottom', offset: -4, fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} unit="%" width={40} />
              <Tooltip formatter={(v) => [`${v}%`, 'Recovery rate']} labelFormatter={(l) => `Round ${l}`} />
              <Line type="monotone" dataKey="rate" stroke="var(--brand)" strokeWidth={2} dot={{ r: 2 }} />
            </LineChart>
          </ResponsiveContainer>

          <div style={{ margin: '14px 0 6px', fontSize: 13 }}>
            Bandit independently learned <strong>{data.match_count}/{data.total_segments}</strong> segments'
            best action matching the oracle ground truth:
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table className="table">
              <thead>
                <tr><th>Decline reason</th><th>Bandit learned</th><th>Oracle best</th><th>Match?</th></tr>
              </thead>
              <tbody>
                {data.learned_mapping.map((m) => (
                  <tr key={m.decline_reason}>
                    <td>{m.decline_reason}</td>
                    <td>{m.bandit_learned}</td>
                    <td>{m.oracle_best}</td>
                    <td>{m.match ? <span className="badge badge-success">✓</span> : <span className="badge badge-danger">✗</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
