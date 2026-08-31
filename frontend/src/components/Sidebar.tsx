import type { CreateRunParams } from '../api'

interface Props {
  params: CreateRunParams
  setParams: (p: CreateRunParams) => void
  capacityHours: number
  setCapacityHours: (n: number) => void
  onGenerate: () => void
  loading: boolean
}

const AGENTIC_MAX_N = 80

export function Sidebar({ params, setParams, capacityHours, setCapacityHours, onGenerate, loading }: Props) {
  const update = (patch: Partial<CreateRunParams>) => setParams({ ...params, ...patch })

  const setPolicyMode = (mode: CreateRunParams['policy_mode']) => {
    // Agentic mode makes one real LLM call per pursued event -- the backend
    // rejects n > 80 for it (see backend/main.py), so clamp here too rather
    // than let the user hit a 400 after clicking Generate.
    const n = mode === 'agentic' ? Math.min(params.n, AGENTIC_MAX_N) : params.n
    setParams({ ...params, policy_mode: mode, n })
  }

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
          type="range" min={100} max={params.policy_mode === 'agentic' ? AGENTIC_MAX_N : 2000}
          step={params.policy_mode === 'agentic' ? 10 : 100} value={params.n}
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
          onChange={(e) => setPolicyMode(e.target.value as CreateRunParams['policy_mode'])}
        >
          <option value="deterministic">Deterministic (diagnosis-informed)</option>
          <option value="bandit">Thompson Sampling bandit</option>
          <option value="agentic">Agentic (LLM picks, bounded by compliance)</option>
        </select>
        {params.policy_mode === 'agentic' && (
          <div className="field-hint">
            The LLM chooses the action itself from a compliance-filtered candidate
            list per case (real Groq/Gemini calls, capped at {AGENTIC_MAX_N} events/batch).
            No key configured → falls back to the deterministic policy's top pick, cleanly.
          </div>
        )}
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
