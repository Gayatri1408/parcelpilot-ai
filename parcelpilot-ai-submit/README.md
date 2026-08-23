# ParcelPilot AI Support Console

An AI agent for ParcelPilot support, built on the supplied data pack (policies, SOPs,
product docs, customer agreements, and account/order/ticket data).

Supports **both** user contexts from one app: switch between "Customer" (scoped to one
account) and "Staff" (full access + a proactive insights dashboard) in the sidebar.

## Setup

```bash
cd parcelpilot-ai/backend
pip install -r requirements.txt
cp .env.example .env        # then paste your GEMINI_API_KEY into .env
python ingest.py            # builds parcelpilot.db + the BM25 document index
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 — the backend serves the frontend directly, so
there's nothing else to start.

Get a free Gemini key at https://aistudio.google.com/app/apikey (no card
required). Default model is `gemini-3.6-flash`; change `GEMINI_MODEL` in
`.env` to any Gemini chat model that supports tool calling.

## Project layout

```
backend/
  app/
    main.py          FastAPI routes (chat, action confirmation, insights, accounts)
    agent.py          Tool-calling loop against the Gemini API
    auth.py           Mocked caller context + access-control checks
    insights.py        Problem 1: proactive issue detection (rule-based)
    tools/
      doc_search.py     Tool 1: BM25 search over policies/SOPs/agreements
      structured_data.py Tool 2: account/order/ticket lookups + deterministic calculators
      actions.py         Tool 3: propose/execute state-changing actions (mocked, gated)
  data/               Source documents (as .txt) + doc_registry.json (authority metadata)
                       + the original xlsx
  ingest.py           Builds parcelpilot.db and the index/ folder from data/
frontend/
  index.html          Single-file chat console (no build step)
docs/
  ARCHITECTURE.md
  PRODUCT_NOTE.md
```

## Try it

**As a customer** (pick an account in the sidebar):
- "Can I cancel ORD-1001 without a fee?"
- "A pickup is 3 hours late because of carrier fault. Do I get a credit?"

**As staff**:
- "Is TKT-501 breaching SLA? Should we escalate?"
- "Create a follow-up task for the bulk upload issue on LumenWorks" (watch for
  the confirmation card — nothing executes until you click Confirm)
- Switch to the "Proactive insights" tab for the SLA/recurring-issue dashboard.
