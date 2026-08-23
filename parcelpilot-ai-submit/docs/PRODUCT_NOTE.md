# Product Note

## Additional client problem chosen: Problem 1 — Proactive Issue Detection
Implemented as a "Proactive insights" tab for staff (`app/insights.py`,
`GET /api/insights`), computed deterministically from the tickets/orders
tables (not an LLM call, so it's fast, reproducible, and auditable):
- **P1 candidates + SLA status** — flags tickets whose text matches
  outage/security keywords, computes elapsed time against the account's
  actual P1 target (contract override if one exists, else plan default),
  and marks a clear breach/ok badge.
- **Recurring / cross-customer issues** — clusters open+historical tickets
  against the two known product issues (bulk upload, SwiftShip webhook
  delay) and flags whether more than one account is affected.
- **Unresolved carrier-fault pickups** — orders still BOOKED with
  carrier-fault marked, i.e. likely-eligible service credits nobody has
  actioned yet.

This directly targets the "reactive chatbot only helps once someone asks"
gap: on the sample data it correctly surfaces that Northstar's P1 outage
ticket has already breached their 15-minute SLA, and that the API-key-
exposure ticket is a live security P1 — both things a busy ops team could
otherwise miss for a while.

## What I'd build next, in priority order
1. **Severity classification for tickets** — today severity is inferred by
   keyword match for the insights view only. A real system needs the chat
   agent and the dashboard to agree on P1/P2/P3, ideally via a small
   classifier or a required field at ticket creation, so SLA tracking is
   accurate for every ticket, not just outage/security-shaped ones.
2. **Real auth + audit log** — replace the mocked role/account picker with
   real sessions, and log every tool call (especially proposed/executed
   actions) for compliance review — the `actions` table is already there,
   it just needs a viewer.
3. **Contract clause extraction pipeline** — right now overrides are
   hand-entered into `CONTRACT_OVERRIDES`. For >2 contracts this needs a
   reviewed extraction step (LLM-assisted, human-approved) so new
   agreements don't require a code change to be enforced correctly.
4. **Feedback loop on historical tickets** — the pack shows two closed
   tickets with wrong resolutions (`TKT-450`, `TKT-451`). A "flag this
   historical answer as incorrect" action, surfaced when the agent
   reconciles a live question against a wrong past answer, would let the
   team clean the corpus over time instead of just working around it.
5. **Multi-channel intake** — tickets currently only enter via
   email/chat fields in the data; wiring real inboxes/webhooks in is out of
   scope here but is the obvious next integration.

## What I intentionally left out
- Vector-embedding retrieval (BM25 is sufficient at this corpus size —
  see architecture note).
- Real staff role/permission tiers beyond a single "staff" role (the
  access-control *pattern* is there; granular roles are a config problem,
  not an architecture one).
- Automated regression tests / CI — I hand-verified the tool layer
  (access control, both calculators, action confirm flow) directly, but
  didn't have time to write a formal test suite.
- Voice/omnichannel UI — a single chat console is enough to demonstrate the
  behavior the assessment asks for.

## One metric I'd use to judge usefulness
**Percentage of customer-facing queries resolved without human
escalation, at a target accuracy of "would a support lead approve this
answer unedited."** Raw deflection rate alone is dangerous here (an agent
could hit 100% by never escalating and guessing) — pairing it with a
sampled human-approval rate on the same population is what keeps that
number honest, which matters a lot given how explicitly this data pack
sets up the trust problem.

## AI tool usage
Built with Claude (Anthropic) inside a sandboxed dev environment: used to
scaffold the FastAPI backend, write the tool/agent/access-control logic, the
frontend console, and this documentation, with each piece smoke-tested
directly (ingest run, tool-layer unit checks, live server + endpoint curls)
rather than taken on faith.
