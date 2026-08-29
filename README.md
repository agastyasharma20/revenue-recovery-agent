# AI Revenue Recovery Agent

Built for the Razorpay AI Buildathon, Track 03 — Revenue Leakage.

An agent that detects failed subscription/mandate payments, abandoned
checkouts, and overdue B2B invoices; diagnoses *why* each one failed; decides
whether recovery is even worth pursuing; picks the best recovery action;
respects compliance stopping rules; and logs every decision in a fully
explainable, tamper-evident audit trail.

This README is written to be checked, not taken on faith: every number below
came from a command you can re-run yourself (see "Reproduce every number"),
and the "What's simulated vs real" section says plainly which parts are a
controlled simulation and which parts touch real infrastructure.

---

## Architecture

```
                          ┌─────────────────────────┐
  Webhook / synthetic --> │   core/ingestion.py      │  idempotency keys:
  batch generator         │   (IngestionGateway)     │  redelivered webhook
                          └───────────┬──────────────┘  is a no-op
                                      │
                                      v
                          ┌─────────────────────────┐
                          │   core/schema.py         │  unified RevenueEvent
                          │   RevenueEvent           │  (subscription_failed /
                          └───────────┬──────────────┘   checkout_abandoned /
                                      │                   b2b_receivable_overdue)
                                      v
     ┌────────────────────────────────────────────────────────┐
     │                    core/engine.py                       │
     │              RecoveryEngine.process_event()              │
     │                                                          │
     │  1. DIAGNOSE   core/classifier.py                        │
     │       rule-based backbone (deterministic, free, instant) │
     │       + optional Groq LLM confidence refinement          │
     │       (circuit-breaker protected, fails closed to rules) │
     │                        │                                 │
     │  2. PRIORITIZE  core/prioritizer.py                       │
     │       EV = P(recovery) * amount - cost                   │
     │       negative EV -> "do not pursue", logged with why    │
     │                        │                                 │
     │  3. COMPLIANCE  core/compliance.py + core/rules.yaml      │
     │       NPCI-style stopping rules (versioned, in YAML):     │
     │       max 3 retries, 24h min gap, 7-day pursuit window,   │
     │       24h mandate-creation cooldown                       │
     │                        │                                 │
     │  4. ACT        core/policy.py                             │
     │       deterministic diagnosis-informed picker (default)   │
     │       OR Thompson Sampling / core/contextual_bandit.py    │
     │       LinUCB (learns purely from outcome feedback,        │
     │       no hardcoded reason->action mapping)                │
     │                        │                                 │
     │  5. OUTCOME    core/outcome_simulator.py                  │
     │       per-(decline_reason, action) effectiveness matrix   │
     │       (harsh on purpose: nonsense actions ~= 0% success)  │
     │                        │                                 │
     │  6. AUDIT      core/audit.py                              │
     │       SHA-256 hash-chained JSONL, verify_chain() detects  │
     │       any alteration/deletion/reordering                 │
     └────────────────────────────────────────────────────────┘
                                      │
              ┌───────────────────────┼────────────────────────┐
              v                       v                        v
   core/anomaly.py           core/portfolio.py         core/metrics.py
   Isolation Forest +        0/1 knapsack (exact DP)   /metrics summary:
   rolling-window             vs greedy top-N-by-EV     recovery rate,
   root-cause clustering      for human-review capacity  per-layer latency,
   -> SYSTEMIC INCIDENT flag                             LLM fallback rate
                                      │
                                      v
                          dashboard/app.py (Streamlit)
```

`webhook_server.py` (Phase 5) sits in front of the same `RecoveryEngine` as a
Flask receiver for real Razorpay `payment.failed` /
`subscription.charged.failed` webhooks, verified via Razorpay's own SDK.

---

## Headline results (from the runs below, not cherry-picked)

**Naive "retry everything once" baseline vs. the full agent pipeline**, same
synthetic batch, 5 random seeds, 500 events each
(`python run_evaluation.py --n 500 --seeds 1 2 3 4 5`):

| | Baseline (retry-everything) | Full agent |
|---|---|---|
| Mean recovery rate (of pursued cases) | 12.2% | **34.3%** |
| Mean net INR recovered (recovered − cost) | ₹167,832 | **₹5,583,250** |
| Wins on net INR, across 5 seeds | — | **5 / 5** |

**Be skeptical of the ₹5.58M vs ₹167K gap** — we were, and dug into it before
reporting it (see "What's simulated vs real" below): it is dominated by B2B
invoice recovery, which the naive baseline structurally cannot capture at all
(retrying a "payment" makes no sense when there's no failed transaction — an
invoice or an abandoned checkout has none). The **recovery-rate** comparison
(34.3% vs 12.2%, consistent across all 5 seeds) is the fairer, source-agnostic
headline; the rupee comparison is real but scale-dominated by whichever
source has the largest amounts in a given batch.

**Contextual bandit (LinUCB) convergence**, 20,000 sequential rounds, one
bandit's state carried forward throughout (`python bandit_convergence_demo.py`):
recovery rate climbs from ~18% (first 500-round window) to a ~35–40% plateau,
and the bandit's independently-learned best action matches the oracle
ground-truth best action on **12/12** decline-reason segments — with **no**
reason→action mapping ever hardcoded into the bandit itself.

**Portfolio optimization (0/1 knapsack vs. greedy top-N by EV)**
(`python portfolio_demo.py --seed 44 --capacity-hours 20`): on a sample batch,
knapsack captured ₹2,769 more EV than greedy top-N (+0.22%) under the same
20-hour human-review budget. **Honestly, this gap is usually small on this
data** (well under 1% of EV, and zero on many seed/capacity combinations) —
because case value and handling-time both scale with deal size here, so
greedy-by-value is already close to optimal most of the time. The script also
runs a hand-built textbook instance where greedy scores 30 and the exact
knapsack solver scores 48, proving the algorithm's value independent of
whether the synthetic data happens to expose it.

**Tests**: 32/32 passing (`python -m pytest tests/ -v`), including audit-chain
tamper detection (altering, deleting, and reordering a record are each
caught and pinpointed), all 4 NPCI-style compliance rules, a seeded
"systemic incident" spike detection, idempotent webhook ingestion, and
Razorpay signature verification.

---

## Reproduce every number

```bash
pip install -r requirements.txt

# Phase 0: baseline vs agent, 5 seeds
python run_evaluation.py --n 500 --seeds 1 2 3 4 5

# Phase 1: contextual bandit convergence + learned-mapping check
python bandit_convergence_demo.py
# -> also writes results/bandit_convergence.png

# Phase 2: anomaly + systemic-incident spike test
python -m pytest tests/test_anomaly.py -v

# Phase 3: knapsack vs greedy, textbook example + real batch
python portfolio_demo.py --seed 44 --capacity-hours 20

# Phase 4: full test suite (compliance, audit chain, anomaly, ingestion, portfolio, razorpay)
python -m pytest tests/ -v

# Phase 6: dashboard
streamlit run dashboard/app.py
```

Every synthetic run is seeded (`--seed`); the same seed always reproduces the
same events and the same outcomes.

---

## What's simulated vs. real — read this before presenting any number

Be explicit about this to yourself and to anyone you show this to.

**Simulated (all headline numbers above come from this):**
- **The events themselves.** `data/generate_synthetic.py` generates every
  subscription failure, abandoned checkout, and overdue invoice used in
  `run_evaluation.py`, `bandit_convergence_demo.py`, `portfolio_demo.py`, and
  the dashboard's default view. No real payments data is involved.
- **Whether a recovery action "succeeds".** `core/outcome_simulator.py` is a
  hand-authored probability table (per decline-reason × action), not
  measured from real recovery campaigns. The probabilities are directionally
  realistic (e.g. retrying an expired card ≈ 0% success) but are estimates,
  not ground truth.
- **The EV priors in `core/prioritizer.py`** are a separate, coarser,
  hand-set belief table — deliberately *not* read from the outcome
  simulator's matrix (that would be an oracle leak that makes the evaluation
  meaningless). Some mismatch between the two is expected and realistic; a
  real system's estimates are never perfectly calibrated either.
- **The hidden ground truth** (`data/generate_synthetic.py`'s
  `GroundTruth` / oracle best-action) exists only for scoring after the
  fact — the agent pipeline never sees it.

**Real (actually exercised, not simulated):**
- **The pipeline logic itself** — rule-based diagnosis, EV math, NPCI-style
  compliance checks, Thompson Sampling / LinUCB bandit math, the 0/1 knapsack
  DP, Isolation Forest anomaly scoring, and the SHA-256 hash-chained audit
  log — all run as real code on real (if synthetic) inputs, not mocked out.
- **The Groq LLM call** in `core/classifier.py`, if you set `GROQ_API_KEY` —
  a real API call to a real model, with a circuit breaker verified (via a
  deliberately invalid key) to trip after 3 consecutive failures and fall
  back to rule-based-only mode without crashing the pipeline.
- **Razorpay webhook signature verification and payload parsing**
  (`core/razorpay_integration.py`) — real HMAC-SHA256 verification via
  Razorpay's own SDK, tested against hand-built payloads matching Razorpay's
  documented webhook shape, including tampered-body and wrong-secret
  rejection.
- **NOT yet tested**: live webhook delivery from an actual Razorpay test-mode
  dashboard over the internet. That requires your own `RAZORPAY_KEY_ID` /
  `RAZORPAY_KEY_SECRET` / `RAZORPAY_WEBHOOK_SECRET` (see `.env.example`) and
  a public tunnel (e.g. `ngrok http 5000`) pointed at `webhook_server.py`. Do
  not present "processed a live Razorpay webhook" as demonstrated until you
  run that yourself — everything else in this repo's test suite uses
  locally-constructed payloads, not Razorpay's servers.
- **The dashboard** runs the same synthetic-data pipeline live in the
  browser session (not pre-recorded numbers), but "live" here means
  "computed when you load the page," not "connected to a production payments
  feed."

---

## Two real bugs caught during development (left in this README on purpose)

Rigor means showing the mistakes, not just the passing tests.

1. **Knapsack scored *worse* than greedy on real data** — impossible for a
   correct exact solver, and the first sign something was wrong. Root cause:
   the DP discretized hours into 0.05h units, but case weights were computed
   to 3-decimal precision — the resulting rounding error let a slightly
   different (and worse) combination look best. Fixed by matching the
   discretization resolution (0.001h) to the actual weight precision, with a
   regression test (`test_knapsack_never_worse_than_greedy_on_real_batches`)
   across 8 seeds × 4 capacities so it can't silently regress.
2. **A naive "LLM fallback rate" metric would have reported 100% fallback
   even with the LLM completely disabled** — technically true (0 used / 0
   attempted looks like "all failed" if you're not careful) but misleading.
   Fixed by tracking `llm_attempted` separately from `llm_used` in
   `core/classifier.py`'s `Diagnosis`, so the fallback rate is only computed
   relative to calls actually attempted.

---

## Repository layout

```
core/
  schema.py            unified RevenueEvent, DiagnosisCategory, Action vocab
  classifier.py         rule-based diagnosis + optional Groq LLM refinement
  prioritizer.py         EV = P(recovery)*amount - cost
  compliance.py           NPCI-style stopping rules (reads rules.yaml)
  rules.yaml               versioned compliance thresholds
  policy.py                deterministic policy + Thompson Sampling bandit
  contextual_bandit.py      LinUCB contextual bandit (Phase 1)
  anomaly.py                Isolation Forest + root-cause clustering (Phase 2)
  portfolio.py               0/1 knapsack portfolio optimization (Phase 3)
  ingestion.py                idempotent webhook ingestion (Phase 4)
  circuit_breaker.py           LLM circuit breaker (Phase 4)
  logging_config.py             structured JSON logging w/ trace_id (Phase 4)
  metrics.py                     /metrics-style summary (Phase 4)
  razorpay_integration.py         Razorpay webhook verify + parse (Phase 5)
  outcome_simulator.py             per-reason x per-action effectiveness matrix
  engine.py                         orchestrates all of the above
data/
  generate_synthetic.py    synthetic events + hidden oracle ground truth
dashboard/
  app.py                   Streamlit dashboard (Phase 6)
tests/                     32 tests across compliance, audit chain, anomaly,
                            portfolio, ingestion, razorpay integration
run_evaluation.py           baseline vs agent, multi-seed
bandit_convergence_demo.py  Phase 1 convergence proof
portfolio_demo.py           Phase 3 knapsack vs greedy proof
webhook_server.py           Phase 5 Flask webhook receiver
.env.example                 template for Razorpay/Groq credentials
```

---

## Setup

```bash
git clone <this-repo>
cd revenue-recovery-agent
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

# optional: enable LLM-refined diagnosis
cp .env.example .env
# then edit .env and set GROQ_API_KEY (free tier: https://console.groq.com/keys)

python -m pytest tests/ -v          # confirm the suite passes
python run_evaluation.py            # headline agent-vs-baseline numbers
streamlit run dashboard/app.py      # interactive dashboard
```

For live Razorpay webhooks (Phase 5), also set `RAZORPAY_KEY_ID`,
`RAZORPAY_KEY_SECRET`, and `RAZORPAY_WEBHOOK_SECRET` in `.env` (from your
Razorpay **test-mode** dashboard), then:

```bash
python webhook_server.py
# in another terminal: ngrok http 5000
# register the ngrok URL + /webhook/razorpay in Razorpay Dashboard -> Settings -> Webhooks
```

`.env` is excluded via `.gitignore` — never commit real keys.
