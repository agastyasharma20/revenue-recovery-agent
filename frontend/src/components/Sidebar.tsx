import type { CreateRunParams } from '../api'

interface Props {
  params: CreateRunParams
  setParams: (p: CreateRunParams) => void
  capacityHours: number
  setCapacityHours: (n: number) => void
  onGenerate: () => void
  loading: boolean
}

export function Sidebar({ params, setParams, capacityHours, setCapacityHours, onGenerate, loading }: Props) {
  const update = (patch: Partial<CreateRunParams>) => setParams({ ...params, ...patch })

  return (
    <aside className="sidebar">
      <div className="brand-row">
        <div className="logo">💸</div>
        <div>
          <div className="brand-title">Revenue Recovery Agent</div>
          <div className="brand-sub">Razorpay AI Buildathon · Track 03</div>
        </div>
      </div>

      <div className="field">
        <label>Events in batch: {params.n}</label>
        <input
          type="range" min={100} max={2000} step={100} value={params.n}
          onChange={(e) => update({ n: Number(e.target.value) })}
        />
      </div>

      <div className="field">
        <label>Random seed</label>
        <input
          type="number" value={params.seed}
          onChange={(e) => update({ seed: Number(e.target.value) })}
        />
      </div>

      <div className="field">
        <label>Action policy</label>
        <select
          className="select-box" value={params.policy_mode}
          onChange={(e) => update({ policy_mode: e.target.value as CreateRunParams['policy_mode'] })}
        >
          <option value="deterministic">Deterministic (diagnosis-informed)</option>
          <option value="bandit">Thompson Sampling bandit</option>
        </select>
      </div>

      <label className="checkbox-row">
        <input
          type="checkbox" checked={params.inject_spike}
          onChange={(e) => update({ inject_spike: e.target.checked })}
        />
        Inject synthetic bank-outage spike (demo)
      </label>

      <label className="checkbox-row">
        <input
          type="checkbox" checked={params.use_llm}
          onChange={(e) => update({ use_llm: e.target.checked })}
        />
        Use Groq LLM for diagnosis refinement
      </label>

      <label className="checkbox-row">
        <input
          type="checkbox" checked={!params.auto_approve}
          onChange={(e) => update({ auto_approve: !e.target.checked })}
        />
        Human-in-the-loop mode (hold high-risk actions for approval)
      </label>

      <div className="field">
        <label>Human-review capacity: {capacityHours}h/day</label>
        <input
          type="range" min={1} max={60} value={capacityHours}
          onChange={(e) => setCapacityHours(Number(e.target.value))}
        />
      </div>

      <button className="btn btn-primary" onClick={onGenerate} disabled={loading}>
        {loading ? 'Running…' : '▶ Generate new batch'}
      </button>

      <div className="sidebar-footer">
        All numbers come from the synthetic generator run through the real core/ pipeline — same code as
        run_evaluation.py. See README for what's simulated vs real.
      </div>
    </aside>
  )
}
