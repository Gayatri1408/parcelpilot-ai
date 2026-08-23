"""
Problem 1: Proactive Issue Detection.

Deliberately rule-based and deterministic (not an LLM call) so the internal
dashboard is fast, reproducible, and auditable -- it flags candidates for a
human to triage, it doesn't decide anything on its own.
"""
import sqlite3
from datetime import datetime

from app.config import DB_PATH, DATASET_SNAPSHOT

SNAPSHOT = datetime.strptime(DATASET_SNAPSHOT, "%Y-%m-%d %H:%M")

# Plan default P1 first-response targets (minutes), from Support Policy v3.
P1_TARGET_MINUTES = {"Enterprise": 30, "Growth": 120, "Standard": 240}
# Contract overrides (minutes) -- Northstar's agreement replaces the plan default.
P1_TARGET_OVERRIDE_MINUTES = {"ACCT-001": 15}

OUTAGE_KEYWORDS = ["all shipment creation is failing", "http 500", "outage", "down"]
SECURITY_KEYWORDS = ["api key", "credential", "security", "exposure", "exposed"]
KNOWN_ISSUE_CLUSTERS = {
    "KI-208 Bulk Upload failures": ["bulk upload", "csv"],
    "KI-211 SwiftShip webhook delay": ["swiftship", "still shows booked", "webhook"],
}


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _minutes_open(created_at: str) -> float:
    return (SNAPSHOT - datetime.strptime(created_at, "%Y-%m-%d %H:%M")).total_seconds() / 60


def build_insights() -> dict:
    conn = _conn()
    tickets = [dict(r) for r in conn.execute("SELECT * FROM tickets").fetchall()]
    accounts = {r["account_id"]: dict(r) for r in conn.execute("SELECT * FROM accounts").fetchall()}
    orders = [dict(r) for r in conn.execute("SELECT * FROM orders").fetchall()]
    conn.close()

    open_tickets = [t for t in tickets if t["status"] == "open"]

    # 1. P1 candidates + SLA breach check
    sla_flags = []
    for t in open_tickets:
        text = f"{t['subject']} {t['description']}".lower()
        is_security = any(k in text for k in SECURITY_KEYWORDS)
        is_outage = any(k in text for k in OUTAGE_KEYWORDS)
        if not (is_security or is_outage):
            continue
        acct = accounts.get(t["account_id"], {})
        plan = acct.get("plan", "Standard")
        target = P1_TARGET_OVERRIDE_MINUTES.get(t["account_id"], P1_TARGET_MINUTES.get(plan, 240))
        elapsed = round(_minutes_open(t["created_at"]), 1)
        sla_flags.append({
            "ticket_id": t["ticket_id"],
            "account_id": t["account_id"],
            "account_name": acct.get("account_name"),
            "subject": t["subject"],
            "candidate_severity": "P1",
            "reason": "security-related" if is_security else "outage-related",
            "minutes_open": elapsed,
            "p1_target_minutes": target,
            "sla_breached": elapsed > target,
        })

    # 2. Recurring / cross-customer product issues
    recurring = []
    for label, keywords in KNOWN_ISSUE_CLUSTERS.items():
        matches = [t for t in tickets
                   if any(k in f"{t['subject']} {t['description']}".lower() for k in keywords)]
        if len(matches) >= 2:
            distinct_accounts = {m["account_id"] for m in matches}
            recurring.append({
                "cluster": label,
                "ticket_count": len(matches),
                "distinct_accounts_affected": len(distinct_accounts),
                "tickets": [{"ticket_id": m["ticket_id"], "account_id": m["account_id"],
                             "status": m["status"], "subject": m["subject"]} for m in matches],
                "cross_customer": len(distinct_accounts) > 1,
            })

    # 3. Order-side anomalies: carrier-fault delays awaiting resolution
    stuck_orders = []
    for o in orders:
        if o["status"] == "BOOKED" and o["carrier_fault"] in ("True", "1", True) \
                and o.get("pickup_actual_at") in (None, "", "None"):
            stuck_orders.append({
                "order_id": o["order_id"], "account_id": o["account_id"],
                "carrier": o["carrier"], "notes": o["notes"],
            })

    return {
        "snapshot_time": DATASET_SNAPSHOT,
        "p1_candidates_and_sla": sla_flags,
        "recurring_or_cross_customer_issues": recurring,
        "unresolved_carrier_fault_pickups": stuck_orders,
        "open_ticket_count": len(open_tickets),
    }
