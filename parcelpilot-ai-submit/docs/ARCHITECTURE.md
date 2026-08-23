# Architecture Note

## Agent design
A single agent (Gemini `gemini-3.6-flash` by default, via Google AI Studio's
OpenAI-compatible tool-calling endpoint) runs a tool-call loop (`app/agent.py`): the model is given a system
prompt encoding source precedence and escalation rules, plus the caller's
context (role + account), and repeatedly calls tools until it can answer
without one. The same agent serves both the customer and staff contexts —
only the `CallerContext` and system-prompt framing differ, not the code path.

## Tool design
Three required tool categories, seven functions:
1. **Document search** — `search_documents` (BM25 over chunked policies/SOPs/
   product docs/agreements). Boosts the caller's own account agreement and
   demotes the deprecated Support Policy v2 so it only surfaces as a
   last resort, always labeled `DEPRECATED`.
2. **Structured data + calculation** — `get_account`, `get_orders`,
   `get_tickets`, plus two deterministic calculators:
   `calculate_cancellation_fee` and `calculate_service_credit`. These are
   **not** left to the LLM to compute — the tool applies SOP v4's rules and
   any contract override in plain Python and returns the number plus the
   rule it used, so the model's job is to explain, not to do arithmetic on
   dates and fees (a place LLMs are unreliable).
3. **State-changing action** — `propose_action` (escalation / ticket update /
   follow-up task, mocked). It only ever stages a row with `status="pending"`.

## Confirmation gate
`execute_action()` is deliberately **not exposed to the model as a tool** —
it's only reachable via `POST /api/actions/{id}/confirm`, which the frontend
calls when the user clicks "Confirm" on the action card. This means the
human-in-the-loop requirement is enforced by the system's shape, not by
hoping the model remembers to ask.

## Document and structured-data handling
Documents are pre-chunked at ingest time (`ingest.py`) into section-sized
pieces with metadata (`doc_registry.json`): status (CURRENT/DEPRECATED/
ACTIVE), authority rank, owning account (for agreements), and effective
date. Structured data (accounts/orders/tickets) is loaded from the xlsx into
SQLite once at ingest time and queried directly — no vector DB, since the
dataset is small and exact filtering by account/order/ticket id matters more
than semantic recall.

## Source reliability and conflict handling
Precedence is explicit in both the system prompt and in code:
signed customer agreement (for the caller's account) > current policy/SOP >
current product docs > historical tickets (context only, may be wrong — this
is demonstrated by the pack itself: `TKT-450` and `TKT-451` contain
resolutions that contradict the current SOP/product limits). The deprecated
policy file is retained and indexed (so the agent can discuss history if
asked) but is excluded from normal answers. Contract overrides are captured
in **two places on purpose**: as indexed text (so the agent can quote/explain
the clause) and as a small `CONTRACT_OVERRIDES` table in
`structured_data.py` (so the number used in a calculation is guaranteed
correct rather than extracted from prose by the model). When required facts
are missing or contradictory (e.g. fault unknown), the calculators return an
explicit `"unknown"`/low-confidence result rather than a number, and the
system prompt instructs the agent to say so and offer escalation instead of
guessing.

## Access control
Enforced in `auth.py` + at the top of every function in `structured_data.py`
and `actions.py` — a customer's `CallerContext` cannot read or act on another
account's rows even if the model tries; the tool raises/403s before any data
leaves the layer. This is independent of prompt instructions.

## Major trade-offs
- **BM25, not embeddings**: the corpus is 6 documents / 24 chunks. A vector
  index adds a dependency and no real benefit at this scale; BM25 is
  transparent and debuggable. This would need to change if the document set
  grew substantially.
- **Hybrid calculation approach**: contract terms live in both text (for
  explanation) and code (for correctness) — a small duplication accepted in
  exchange for calculations the model can't get wrong.
- **Rule-based (not LLM) proactive insights**: keyword clustering, not
  embeddings/LLM classification, so the dashboard is fast and auditable. It
  would miss issues phrased very differently from the known keywords —
  acceptable for a first version, flagged in the product note as the next
  investment.
- **Single mocked auth layer**: role + account are passed by the frontend
  rather than a real session/JWT, appropriate for an assessment; the
  enforcement pattern (check in the data layer, not the prompt) is the part
  meant to generalize.
