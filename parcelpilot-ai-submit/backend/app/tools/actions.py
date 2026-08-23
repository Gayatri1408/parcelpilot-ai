"""
State-changing action tool (mocked). Design choice: the LLM can only ever
PROPOSE an action via propose_action(); it is stored as status="pending".
Execution happens ONLY through execute_action(), which is called by a
dedicated REST endpoint the frontend hits after the human clicks "Confirm" --
the model itself has no tool that can flip a pending action to executed.
This keeps the confirmation gate outside the model's control, not just a
prompt instruction.
"""
import sqlite3
import uuid
from datetime import datetime

from app.config import DB_PATH
from app.auth import CallerContext

VALID_TYPES = {"create_escalation", "update_ticket", "create_followup_task"}


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table():
    conn = _conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS actions (
            action_id TEXT PRIMARY KEY,
            action_type TEXT,
            account_id TEXT,
            payload TEXT,
            summary TEXT,
            status TEXT,
            proposed_by TEXT,
            proposed_at TEXT,
            executed_at TEXT
        )
    """)
    conn.commit()
    conn.close()


_ensure_table()


def propose_action(caller: CallerContext, action_type: str, account_id: str,
                    payload: dict, summary: str) -> dict:
    if action_type not in VALID_TYPES:
        return {"error": f"Unsupported action_type '{action_type}'. Must be one of {VALID_TYPES}."}
    caller.assert_can_access_account(account_id)
    if caller.role != "staff" and action_type != "create_escalation":
        return {"error": "Customers may only request an escalation; other actions require staff."}

    action_id = f"ACT-{uuid.uuid4().hex[:8]}"
    conn = _conn()
    conn.execute(
        "INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?)",
        (action_id, action_type, account_id, str(payload), summary, "pending",
         caller.staff_name or "customer", datetime.utcnow().isoformat(), None),
    )
    conn.commit()
    conn.close()
    return {
        "action_id": action_id,
        "status": "pending_confirmation",
        "summary": summary,
        "message": "This action has NOT been executed yet. Ask the user to confirm "
                    "before it is carried out.",
    }


def execute_action(action_id: str) -> dict:
    conn = _conn()
    row = conn.execute("SELECT * FROM actions WHERE action_id = ?", (action_id,)).fetchone()
    if not row:
        conn.close()
        return {"error": f"No pending action {action_id}"}
    if row["status"] != "pending":
        conn.close()
        return {"error": f"Action {action_id} is already {row['status']}."}
    conn.execute(
        "UPDATE actions SET status = 'executed', executed_at = ? WHERE action_id = ?",
        (datetime.utcnow().isoformat(), action_id),
    )
    conn.commit()
    conn.close()
    return {"action_id": action_id, "status": "executed"}


def get_action(action_id: str) -> dict:
    conn = _conn()
    row = conn.execute("SELECT * FROM actions WHERE action_id = ?", (action_id,)).fetchone()
    conn.close()
    return dict(row) if row else {"error": "not found"}
