import { useEffect, useState } from 'react'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell } from 'recharts'
import { api } from '../api'
import type { PortfolioResult } from '../types'

export function PortfolioComparison({ runId, capacityHours }: { runId: string; capacityHours: number }) {
  const [data, setData] = useState<PortfolioResult | null>(null)

  useEffect(() => {
    api.getPortfolio(runId, capacityHours).then(setData)
  }, [runId, capacityHours])

  if (!data) return null

  if (data.cases_available === 0) {
    return (
      <div className="card">
        <div className="section-title"><span className="emoji">🎒</span><span>Portfolio optimization</span></div>
        <p className="loading-text">No human-escalation cases in this batch to optimize a portfolio over.</p>
      </div>
    )
  }

  const chartData = [
    { name: 'Top-N by EV (greedy)', value: data.topn!.value },
    { name: 'Knapsack-optimal', value: data.knapsack!.value },
  ]

  return (
    <div className="card">
      <div className="section-title">
        <span className="emoji">🎒</span>
        <span>Portfolio optimization: top-N by EV vs. knapsack-optimal</span>
      </div>
      <p style={{ fontSize: 12.5, color: 'var(--text-muted)', marginBottom: 14 }}>
        {data.cases_available} cases need human escalation today; capacity is {capacityHours}h. 0/1 knapsack
        (exact DP) selects the value-maximizing subset within that hour budget, instead of greedily ranking by EV alone.
      </p>

      <ResponsiveContainer width="100%" height={180}>
        <BarChart data={chartData} layout="vertical" margin={{ left: 40 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" horizontal={false} />
          <XAxis type="number" tick={{ fontSize: 11 }} tickFormatter={(v) => `₹${(v / 1000).toFixed(0)}k`} />
          <YAxis type="category" dataKey="name" tick={{ fontSize: 12 }} width={140} />
          <Tooltip formatter={(v) => [`₹${Number(v).toLocaleString('en-IN')}`, 'EV captured']} />
          <Bar dataKey="value" radius={[0, 6, 6, 0]}>
            <Cell fill="#94a3b8" />
            <Cell fill="var(--brand)" />
          </Bar>
        </BarChart>
      </ResponsiveContainer>

      <div className="two-col" style={{ marginTop: 14 }}>
        <div className="stat-card">
          <div className="stat-label">Top-N (greedy)</div>
          <div className="stat-value mono" style={{ fontSize: 18 }}>{data.topn!.selected} cases · ₹{data.topn!.value.toLocaleString('en-IN')}</div>
          <div style={{ fontSize: 11.5, color: 'var(--text-faint)' }}>{data.topn!.hours_used.toFixed(1)}h used</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Knapsack-optimal</div>
          <div className="stat-value mono" style={{ fontSize: 18 }}>{data.knapsack!.selected} cases · ₹{data.knapsack!.value.toLocaleString('en-IN')}</div>
          <div style={{ fontSize: 11.5, color: 'var(--text-faint)' }}>{data.knapsack!.hours_used.toFixed(1)}h used</div>
        </div>
      </div>

      <p style={{ fontSize: 12.5, marginTop: 10 }}>
        Knapsack advantage: <strong>₹{data.value_gain!.toLocaleString('en-IN')}</strong> ({data.value_gain_pct!.toFixed(2)}%).{' '}
        {data.value_gain === 0 && (
          <span style={{ color: 'var(--text-faint)' }}>
            No gap on this batch/capacity combo — greedy found the optimum here (this can happen; the DP
            guarantee is that it never does worse, not that it always beats greedy by a lot — see README).
          </span>
        )}
      </p>
    </div>
  )
}
