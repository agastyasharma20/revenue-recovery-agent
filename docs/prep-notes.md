# Prep notes (not part of the public README on purpose)

This is rehearsal material — the pitch-video shot list and the anticipated
Q&A — split out from `README.md` because a judge skimming the repo wants
architecture and verifiable numbers first, not a script for a video they'll
watch separately or a cheat sheet for a conversation they'll have live.
Everything here is still true and still useful, just relocated.

---

## Judge Q&A cheat sheet

**"Is this number real or simulated?"** — Every number in the README says
so explicitly in "What's simulated vs real". Short version: the decision
logic is 100% real code; the payments data and outcomes are a controlled
simulation; the Groq LLM calls are real API calls with measured (not
assumed) success rates; Razorpay webhook signature verification is real;
live Razorpay/WhatsApp/telephony delivery is not yet tested.

**"Why does the agent recover so much more money than the baseline?"** — See
the honesty note under Headline results: it's almost entirely B2B invoice
recovery, which a naive retry-everything baseline structurally cannot touch.
The recovery-*rate* comparison (36.9% vs 11.0%) is the fairer number and
still favors the agent by more than 3x, consistently across seeds.

**"Couldn't the bandit just be memorizing the outcome simulator's
probabilities?"** — No: it only ever sees a reward signal (recovered:
0/1) per (context, action) pair, never the probability itself, and it's
handed the *full* action set every round with zero reason→action mapping
coded anywhere in `core/contextual_bandit.py` or `core/policy.py`'s
`BANDIT_ARMS`. What it converges to is compared *against* the oracle
matrix purely for scoring, after the fact.

**"What happens if the LLM API is down?"** — `core/circuit_breaker.py` trips
after 3 consecutive failures and stops calling it for a cooldown window;
the rule-based classifier keeps the whole pipeline running regardless. This
is tested with a genuinely invalid key, not asserted.

**"What stops the agent from retrying someone forever, or contacting them
at 3am?"** — `core/compliance.py` + `core/rules.yaml`: max 3 retries, 24h
min gap between retries, a 7-day (subscriptions) or 90-day (B2B) pursuit
window, and a 24h post-mandate-creation cooldown. All versioned in YAML,
not hardcoded, so a compliance team could review/change them without a
code deploy.

**"What stops it from auto-approving a huge or fraud-flagged action?"** —
`core/approval.py`: collections escalations over ₹1,00,000, discounts over
₹20,000, and anything diagnosed as a risk block (suspected fraud) require
human sign-off in HITL mode (`RecoveryEngine(auto_approve=False)`) — the
action is proposed and logged but not executed until approved or rejected.

**"Is the audit trail actually tamper-evident, or just a log file?"** —
SHA-256 hash chain: each record embeds the previous record's hash.
`tests/test_audit_chain.py` proves altering, deleting, or reordering any
single record is detected and pinpointed to the exact index.

**"Where did you get stuck?"** — Integrating the Groq LLM call, and it took
three separate misdiagnoses to actually fix. First symptom: every call
failed with a generic network error even with a real key. Root cause:
Groq's Cloudflare front-end blocks `urllib`'s default User-Agent before the
request ever reaches Groq's API (`core/llm_client.py`, the `User-Agent`
header on both provider calls). Fixed that, got a *different* failure: the
originally-hardcoded model had been retired from Groq's lineup entirely —
found by calling `GET /openai/v1/models` directly instead of guessing.
Fixed that, got a *third* failure: the replacement is a reasoning model
that can spend its entire token budget on hidden reasoning and return
empty content, which looked like a parsing bug until I checked
`finish_reason` and `usage.reasoning_tokens` in the raw response. Each
fix revealed the next problem instead of resolving the whole thing, which
is what actually made it "stuck" rather than just a one-line bug — the
same root-cause discipline is what later caught the ML model's misleading
recall number (see "Real bugs caught" in the README) and a real async race
in the React frontend. All three Groq fixes now live in one shared module
(`core/llm_client.py`) specifically so they can't be silently re-broken by
a second call site, which is exactly what happened when `voice_recovery.py`
was added afterward and needed the same fixes.

**"You mentioned Razorpay has an official MCP server
(`razorpay/razorpay-mcp-server`) — did you use it?"** — Not that specific
server: `core/payment_links.py` and `core/razorpay_integration.py` still
call Razorpay's REST API directly via their Python SDK, not through MCP.
But the underlying question — "does the model ever get to decide, or does
deterministic code always decide for it?" — now has a real answer:
`core/agentic_policy.py` (`policy_mode="agentic"`) lets an LLM genuinely
choose the recovery action via a tool-call-style JSON response, not just
generate text. The bound that keeps this safe: the model is only ever
shown actions `core/compliance.py` has *already* approved for that exact
case, and an out-of-list response (hallucinated or real-but-not-offered)
is rejected and falls back to the deterministic policy's top pick, never
executed — `tests/test_agentic_policy.py::test_out_of_bounds_llm_response_is_rejected_not_executed`
proves this directly by simulating exactly that attempt. Routing this
through Razorpay's actual MCP server specifically (so the tool call is a
real MCP tool invocation, not our own JSON contract) is the next natural
step, genuinely not attempted — but "the model decides, bounded" is done.

**"Is the live demo actually deployed, or just localhost?"** — It's a real
deployment: https://revenue-recovery-agent-5b31.onrender.com, one FastAPI
process (Render free tier) serving both the API and the built React
dashboard — see `render.yaml`. Free tier sleeps after 15 minutes idle, so
open the link a minute before demoing it live to avoid a cold-start wait
in front of a judge. Independently smoke-tested end-to-end against the
live URL (not just localhost) before submission: every REST endpoint, all
three policy modes including the agentic n≤80 cap actually rejecting an
oversized batch, the full approve/reject HITL workflow, a real Razorpay
payment link, a real Groq voice script, and the WebSocket live-replay.

**"Did you look at what other teams built?"** — Yes, deliberately, across
all five tracks (not just Track 03) — surveyed roughly 140 buildathon
repos, read 13 of the most substantive ones closely. Three things came
out of that directly: named India-specific compliance rules (RBI e-mandate
pre-debit notice, TRAI-style quiet hours — `rules.yaml` v3, one
competitor had these explicitly named and it's a cheap, credible add),
real ops alerting via Slack webhook (`core/alerting.py` — a feature
category almost nobody had), and SHAP explainability on the recovery
model (one competitor's README explicitly apologized for skipping it).
Equally deliberate: what *didn't* get added — a couple of submissions
used Z3 formal verification or full-duplex LiveKit voice, genuinely
impressive but the wrong track or too large a lift for the time left, so
they're named here rather than half-built into the repo.

---

## 5-minute pitch video outline

A suggested shot list, timed for a 5-minute cap:

1. **(0:00–0:30) The problem, in one line.** State the Track 03 bar verbatim
   on screen: *"Don't just identify the problem. Show measured money
   recovered across a batch, with compliant escalation, stopping rules, and
   an audit trail."* Say this is exactly what you built to answer.
2. **(0:30–1:15) Architecture, fast.** Show the ASCII diagram (or redraw it
   simply) — ingest → diagnose → prioritize → compliance → approve → act →
   outcome → audit. One sentence per stage, no more.
3. **(1:15–2:30) Live demo in the React dashboard** (the deployed link, not
   localhost). Load it, point at: the systemic-incident banner (mention it
   collapses 15 individual failures into one incident), click "Replay live"
   and let the counter animate, drill into one case and show the full
   diagnosis → EV → compliance → action → hash-chain trail, generate a
   Hinglish voice script live.
4. **(2:30–3:15) The three things that make this more than a reminder bot.**
   Show the bandit convergence chart (12/12 learned-vs-oracle match, no
   hardcoded mapping), the knapsack-vs-greedy comparison, and switch to
   agentic mode live — one case, watch the LLM's real rationale come back,
   then say the bound out loud: it only ever saw compliance-approved
   options, and an out-of-bounds pick gets rejected, not executed. Say the
   numbers plainly, including where the knapsack gap is small — that
   honesty is a feature, not a weakness.
5. **(3:15–4:00) Governance.** Toggle HITL mode, show the approval queue
   with real pending cases, approve one and reject one live.
6. **(4:00–4:40) The numbers, and the honesty about them.** State the
   36.9% vs 11.0% recovery-rate result, and immediately name the B2B-driven
   rupee-scale caveat before a judge can ask about it.

### Literal script (final, read-aloud version — ~4:40, safer margin under 5:00)

Prep before hitting record:
- Open the live demo (https://revenue-recovery-agent-5b31.onrender.com), let it wake up
  (free tier sleeps — first load takes 30–50s), run one throwaway batch to warm it.
- Open the architecture page: https://claude.ai/code/artifact/be6bd588-7894-4112-b561-c655a02285b7
  — this is what to show during the 0:20–1:00 segment instead of raw README text.
- Close everything else (notifications, other tabs).

**[0:00–0:20] Hook.**
> "Razorpay's brief for Track 03 says: don't just detect the problem — show
> measured money recovered across a batch, with compliant escalation,
> stopping rules, and an audit trail. That's exactly what I built to
> answer, and everything I'm about to show is live, not a mockup."

**[0:20–1:00] Architecture.** *(show the architecture page)*
> "Every case goes through the same seven stages. Diagnose why it failed.
> Prioritize with expected value — is it even worth chasing. Check
> compliance — NPCI retry limits, RBI's pre-debit notice rule, TRAI quiet
> hours. Pick an action — three different ways, I'll show one in a minute.
> Get human approval if it's consequential. Simulate the outcome. Log it to
> a tamper-evident, hash-chained audit trail. No stage is skipped, and
> every one is logged."

**[1:00–2:10] Live dashboard.** *(switch to the live tab, generate a fresh batch, bandit mode)*
> "This is running right now at revenue-recovery-agent-5b31 on Render — not
> localhost. I'll generate a batch... here's the incident banner — it just
> collapsed fifteen individual bank-timeout failures into one systemic
> incident instead of fifteen separate alerts. Let me drill into one case."
*(click a case, scroll diagnosis → EV → compliance → action → hash)*
> "Full trail: why it was diagnosed this way, the expected-value math on
> whether to chase it, the compliance check, the action chosen, and its
> exact hash in the audit chain — verifiable, not just logged."

**[2:10–3:10] What makes this more than a reminder bot.** *(switch policy mode to Agentic)*
> "Three things go beyond the brief. First: in this mode, a real LLM call
> chooses the action itself — not just generates text. But it's bounded —
> the model only ever sees actions compliance has already approved for this
> case. If it tries anything else, it's rejected and falls back
> automatically — that's tested, not just claimed."
*(switch to bandit convergence chart)*
> "Second: this bandit learned which action works best for each failure
> type from scratch, zero hardcoded rules, and it matches expert ground
> truth on twelve of twelve segments. Third, and maybe most important:
> every claim here is something you can re-run yourself. Seven real bugs,
> found and fixed in the open, are documented in the README — not hidden."

**[3:10–3:45] Governance.** *(toggle HITL, show approval queue)*
> "Big or risky actions don't fire automatically. This collections case is
> over a lakh, so it's paused for a human." *(approve one, reject one)*
> "Approve — it executes. Reject — it never runs. AI proposes, a human
> authorizes."

**[3:45–4:20] The numbers, honestly.**
> "Headline result: 36.9% recovery rate against 11% for a naive
> retry-everything bot, consistent across five random seeds, net INR won on
> all five. And the caveat before you ask: the rupee gap looks huge because
> B2B invoices dominate it — the recovery-rate number is the fair
> comparison, and it still wins by more than 3x. 111 tests passing, all
> reproducible from the README."

**[4:20–4:40] Close.**
> "The LLM calls, the Razorpay payment links, the webhook verification —
> all real. The only thing simulated is the underlying payments data
> itself, stated plainly in the README. Code's at
> github.com/agastyasharma20/revenue-recovery-agent. Thanks for watching."
7. **(4:40–5:00) Close.** One sentence on what's real vs simulated (LLM
   calls and Razorpay signature verification are real; payments data is a
   controlled simulation), and where the code and live link live.
