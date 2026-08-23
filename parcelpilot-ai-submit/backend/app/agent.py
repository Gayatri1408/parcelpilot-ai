import json
import re
import time
import requests

from app.config import GEMINI_API_KEY, GEMINI_URL, GEMINI_MODEL
from app.auth import CallerContext
from app.tools import doc_search, structured_data, actions

# Gemini exposes an OpenAI-compatible /chat/completions endpoint with
# function calling, so the request/response handling and tool schema below
# are plain OpenAI-style -- no Gemini-specific request format needed.

# Free-tier rate limits are still finite, and a multi-tool agent loop feeds
# tool results back into every subsequent request, so usage compounds fast.
# These two knobs keep us under that ceiling instead of just hoping we don't
# hit it:
MAX_TOOL_RESULT_CHARS = 1200      # truncate any single tool result before it re-enters the prompt
MAX_HISTORY_MESSAGES = 12         # only send the most recent turns, not the whole chat
MAX_RETRIES = 4                   # retries specifically for 429s, with backoff


def _truncate_result(result: dict) -> dict:
    """Keeps tool results informative but bounded, so a big doc_search or
    get_orders payload doesn't itself burn most of the TPM budget."""
    text = json.dumps(result, default=str)
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return result
    return {
        "_truncated": True,
        "preview": text[:MAX_TOOL_RESULT_CHARS],
        "note": "Result truncated to conserve tokens; call the tool again with a "
                "narrower query/top_k if you need more detail.",
    }


def _trim_history(messages: list[dict]) -> list[dict]:
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return messages
    return messages[-MAX_HISTORY_MESSAGES:]


def _extract_error_detail(resp: requests.Response) -> str:
    """Error bodies aren't shaped consistently across providers -- Groq uses
    {"error": {"message": ...}}, but Gemini's OpenAI-compat layer sometimes
    returns {"error": [...]}, or the message field can itself be a list/dict.
    Always fall back to raw text rather than raising on an unexpected shape."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text

    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        msg = err.get("message", err)
    else:
        msg = err if err is not None else body

    if isinstance(msg, (dict, list)):
        return json.dumps(msg)
    return str(msg) if msg is not None else resp.text


def _parse_retry_after(resp: requests.Response, detail: str) -> float:
    """Some providers' 429 bodies include an explicit wait time (e.g.
    'try again in 1.395s.') -- prefer that exact figure over a guess; fall
    back to the Retry-After header, then 2s."""
    m = re.search(r"try again in ([\d.]+)s", detail or "")
    if m:
        return float(m.group(1)) + 0.25  # small safety margin
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    return 2.0

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": "Search ParcelPilot policies, SOPs, product documentation, and "
                            "signed customer agreements. Use this for any question about "
                            "rules, terms, SLAs, capabilities, or known issues.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "top_k": {"type": "integer", "description": "Number of results (default 3). "
                                                                  "Keep this small to conserve tokens; "
                                                                  "only raise it if 3 results weren't enough."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_account",
            "description": "Look up a ParcelPilot account by account_id.",
            "parameters": {
                "type": "object",
                "properties": {"account_id": {"type": "string"}},
                "required": ["account_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_orders",
            "description": "Look up orders/shipments, by account_id and/or order_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "order_id": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_tickets",
            "description": "Look up support tickets, by account_id, ticket_id, and/or status. "
                            "Historical ticket resolutions are context only and may be wrong -- "
                            "never treat them as policy.",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "ticket_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["open", "closed"]},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_cancellation_fee",
            "description": "Deterministically calculate whether an order can be cancelled and "
                            "what fee applies, applying any contract override.",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_service_credit",
            "description": "Deterministically calculate failed-pickup service-credit "
                            "eligibility and amount for an order, applying any contract override.",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_action",
            "description": "PROPOSE a state-changing action (escalation, ticket update, or "
                            "follow-up task). This does NOT execute the action -- it only "
                            "stages it and returns a pending action_id. You must show the "
                            "user the summary and get explicit confirmation before telling "
                            "them it's done. The user confirms via a button in the UI.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action_type": {
                        "type": "string",
                        "enum": ["create_escalation", "update_ticket", "create_followup_task"],
                    },
                    "account_id": {"type": "string"},
                    "payload": {"type": "object", "description": "Action-specific details."},
                    "summary": {"type": "string", "description": "Human-readable summary shown "
                                                                   "to the user for confirmation."},
                },
                "required": ["action_type", "account_id", "payload", "summary"],
            },
        },
    },
]

SYSTEM_PROMPT = """You are the ParcelPilot support assistant.

CALLER CONTEXT: {caller_context}
DATASET SNAPSHOT TIME (use as "now" for all time reasoning): {snapshot}

SOURCE PRECEDENCE (highest authority first) -- always follow this order and say so when it matters:
1. A signed customer agreement for the caller's own account (from search_documents).
2. The current Support Policy (v3) and current Cancellation & Service Credit SOP (v4).
3. Current Product Operations Guide (known issues, plan capabilities).
4. Historical support tickets and internal notes -- CONTEXT ONLY. They may contain
   incorrect past guidance (e.g. a wrong fee or wrong row limit told to a customer
   before). Never repeat a historical ticket's resolution as if it were policy;
   verify it against current sources first.
NEVER use Support Policy v2 (marked DEPRECATED) as a current rule. If it surfaces in
search results, ignore it for current questions.

RULES:
- For anything involving fees, cancellations, or service credits, call the calculation
  tools (calculate_cancellation_fee / calculate_service_credit) rather than computing by
  hand -- they already encode the SOP and contract overrides correctly.
- If sources conflict, or a fact needed to answer (e.g. carrier fault, timing) is
  missing or ambiguous, say so plainly and either ask a clarifying question or
  recommend escalation to a human -- do not guess or promise an outcome.
- Only ever propose a state-changing action via propose_action; never claim an action
  is done until the tool result AND the user's explicit confirmation both occur. After
  calling propose_action, tell the user what you're about to do and that you're
  waiting for their confirmation (a Confirm button will appear in the UI).
- Escalate immediately (recommend/propose an escalation) for: P1 severity issues,
  breached response-time targets, anything requiring human judgment or an exception
  not covered by a documented rule, or anything outside your tools' capabilities.
- Customers can only ever see their own account's data -- this is enforced by the
  tools themselves, so if a tool denies access, tell the user you can't share that
  rather than working around it.
- Be concise, cite which source/tool grounds each claim, and never state a policy
  number from memory -- always ground it in a tool call.
"""


def _dispatch_tool(name: str, args: dict, caller: CallerContext) -> dict:
    """Never lets an exception escape -- a tool failure becomes a normal
    {"error": ...} result the model can react to (and the trust layer can
    flag), rather than a 500 that kills the whole chat request."""
    try:
        if name == "search_documents":
            return {"results": doc_search.search_documents(
                query=args["query"], top_k=args.get("top_k", 3),
                account_id=caller.account_id)}
        if name == "get_account":
            return structured_data.get_account(caller, args["account_id"])
        if name == "get_orders":
            return {"orders": structured_data.get_orders(
                caller, args.get("account_id"), args.get("order_id"))}
        if name == "get_tickets":
            return {"tickets": structured_data.get_tickets(
                caller, args.get("account_id"), args.get("ticket_id"), args.get("status"))}
        if name == "calculate_cancellation_fee":
            return structured_data.calculate_cancellation_fee(caller, args["order_id"])
        if name == "calculate_service_credit":
            return structured_data.calculate_service_credit(caller, args["order_id"])
        if name == "propose_action":
            return actions.propose_action(
                caller, args["action_type"], args["account_id"],
                args.get("payload", {}), args["summary"])
        return {"error": f"Unknown tool {name}"}
    except (PermissionError, KeyError, TypeError, ValueError) as e:
        return {"error": str(e)}
    except Exception as e:  # last-resort guard, still keeps the chat alive
        return {"error": f"Tool '{name}' failed unexpectedly: {e}"}


def run_agent(messages: list[dict], caller: CallerContext, max_steps: int = 6) -> dict:
    """Runs the tool-calling loop. `messages` is prior chat history (user/assistant
    turns only, no system prompt). Returns {reply, trace} where trace lists which
    tools were called, for the UI to display."""
    if not GEMINI_API_KEY:
        return {
            "reply": "The server is missing a GEMINI_API_KEY. Get a free key at "
                     "https://aistudio.google.com/app/apikey, add it to backend/.env, "
                     "and restart the server.",
            "trace": [],
            "trust": {"level": "error", "reasons": ["No LLM API key configured."]},
        }

    caller_ctx_str = (f"role=customer, account_id={caller.account_id}"
                       if caller.role == "customer"
                       else f"role=staff, staff_name={caller.staff_name}")
    system = SYSTEM_PROMPT.format(caller_context=caller_ctx_str, snapshot="2026-08-16 11:00 Asia/Kolkata")

    convo = [{"role": "system", "content": system}] + _trim_history(messages)
    trace = []

    for _ in range(max_steps):
        resp = None
        detail = ""
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    GEMINI_URL,
                    headers={"Authorization": f"Bearer {GEMINI_API_KEY}", "Content-Type": "application/json"},
                    json={"model": GEMINI_MODEL, "messages": convo, "tools": TOOLS, "tool_choice": "auto",
                          "temperature": 0.2},
                    timeout=60,
                )
            except requests.RequestException as e:
                return {"reply": f"Couldn't reach the Gemini API: {e}", "trace": trace,
                        "trust": {"level": "error", "reasons": ["LLM request failed"]}}

            if resp.status_code != 429:
                break  # success or a non-rate-limit error -- stop retrying

            detail = _extract_error_detail(resp)
            if attempt == MAX_RETRIES:
                break
            time.sleep(_parse_retry_after(resp, detail))

        if resp.status_code != 200:
            detail = _extract_error_detail(resp)
            if resp.status_code == 429:
                return {"reply": "The assistant is at its request-rate limit right now "
                                 "(free-tier Gemini quota). Please try again in a few seconds -- "
                                 "if this keeps happening, space out requests or check your quota "
                                 "at https://aistudio.google.com/.",
                        "trace": trace,
                        "trust": {"level": "error", "reasons": ["LLM rate limit (429) after retries"]}}
            # Surface Gemini's actual error (bad model name, bad key, etc.) instead
            # of letting FastAPI turn this into an opaque 500 HTML page.
            return {"reply": f"The LLM provider returned an error ({resp.status_code}): {detail}",
                    "trace": trace, "trust": {"level": "error", "reasons": ["LLM provider error"]}}

        data = resp.json()
        choice = data["choices"][0]["message"]
        convo.append(choice)

        tool_calls = choice.get("tool_calls")
        if not tool_calls:
            reply = choice.get("content", "")
            return {"reply": reply, "trace": trace, "trust": _assess_trust(trace, reply)}

        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            result = _dispatch_tool(name, args, caller)
            # Keep the full, untruncated result in the trace (for the UI / trust
            # assessment), but send a token-bounded version back to the model.
            trace.append({"tool": name, "args": args, "result": result})
            convo.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(_truncate_result(result), default=str),
            })

    reply = ("I wasn't able to finish reasoning about this within the tool-call limit "
              "-- could you rephrase or narrow the question?")
    return {"reply": reply, "trace": trace, "trust": _assess_trust(trace, reply)}


def _assess_trust(trace: list[dict], reply: str) -> dict:
    """Rolls up everything the agent touched this turn into a single trust
    signal for the UI. This is deliberately rule-based over the tool trace
    (not another LLM call asking "how sure are you") -- it reflects what
    actually happened, not a self-reported confidence score."""
    reasons = []
    level = "verified"

    deprecated_used = any(
        r["tool"] == "search_documents"
        and any(d.get("status") == "DEPRECATED" for d in r["result"].get("results", []))
        for r in trace
    )
    agreement_used = any(
        r["tool"] == "search_documents"
        and any(d.get("doc_type") == "customer_agreement" for d in r["result"].get("results", []))
        for r in trace
    )
    calc_unknown = any(
        r["tool"] in ("calculate_cancellation_fee", "calculate_service_credit")
        and (r["result"].get("eligible") == "unknown" or "error" in r["result"])
        for r in trace
    )
    access_denied = any("error" in r["result"] and "own account" in str(r["result"].get("error", ""))
                         for r in trace)
    action_pending = any(r["tool"] == "propose_action" and r["result"].get("action_id") for r in trace)
    any_tool_error = any("error" in r["result"] for r in trace if isinstance(r["result"], dict))
    no_grounding = len(trace) == 0

    if deprecated_used:
        level = "needs_review"
        reasons.append("A deprecated document surfaced in search results -- verify it wasn't used.")
    if calc_unknown:
        level = "needs_review"
        reasons.append("A calculation returned an unresolved/unknown result (missing fact).")
    if access_denied:
        reasons.append("An access-control check blocked part of the request (expected, not an error).")
    if action_pending:
        reasons.append("A state-changing action is staged and awaiting your confirmation.")
    if agreement_used and level == "verified":
        reasons.append("Grounded in the caller's signed customer agreement (highest authority).")
    if no_grounding:
        level = "informational"
        reasons.append("No documents or data were looked up -- general/conversational reply.")
    elif level == "verified" and not reasons:
        reasons.append("Grounded in current policy/SOP/product docs, no conflicts detected.")
    if any_tool_error and level == "verified":
        level = "needs_review"
        reasons.append("At least one tool call returned an error.")

    return {"level": level, "reasons": reasons}
