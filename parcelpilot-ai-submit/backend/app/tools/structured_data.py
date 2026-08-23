"""
Structured-data lookup + calculation tool.

Access control is enforced HERE, not left to the model: every function takes
the caller's CallerContext and raises/redacts before any row leaves this
module. Calculations (cancellation fee, service credit) are deterministic
Python, not LLM guesses -- the LLM calls these functions and narrates the
result, it does not compute the numbers itself.
"""
import sqlite3
from datetime import datetime

from app.config import DB_PATH, DATASET_SNAPSHOT
from app.auth import CallerContext

SNAPSHOT = datetime.strptime(DATASET_SNAPSHOT, "%Y-%m-%d %H:%M")

# Contract override parameters mirrored from the two signed agreements.
# (See architecture note: kept structured here for reliable math; the same
# clauses are also indexed for the document-search tool so the agent can
# cite and explain them in natural language.)
CONTRACT_OVERRIDES = {
    "ACCT-001": {  # Northstar Logistics
        "cancellation_fee_waived_if_not_picked_up": True,
        "monthly_service_credit_cap_inr": 5000,
    },
    "ACCT-002": {  # LumenWorks
        "failed_pickup_credit_threshold_hours": 4,
        "failed_pickup_credit_amount_inr": 300,
    },
}


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


def get_account(caller: CallerContext, account_id: str) -> dict:
    caller.assert_can_access_account(account_id)
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    conn.close()
    if not row:
        return {"error": f"No account found with id {account_id}"}
    return _row_to_dict(row)


def get_orders(caller: CallerContext, account_id: str | None = None,
                order_id: str | None = None) -> list[dict]:
    scope = caller.default_account_scope()
    if scope:
        account_id = scope
    elif account_id:
        caller.assert_can_access_account(account_id)

    conn = _conn()
    if order_id:
        rows = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchall()
    elif account_id:
        rows = conn.execute("SELECT * FROM orders WHERE account_id = ?", (account_id,)).fetchall()
    else:
        if caller.role != "staff":
            raise PermissionError("account_id or order_id is required for non-staff callers.")
        rows = conn.execute("SELECT * FROM orders").fetchall()
    conn.close()

    results = [_row_to_dict(r) for r in rows]
    for r in results:
        caller.assert_can_access_account(r["account_id"])
    return results


def get_tickets(caller: CallerContext, account_id: str | None = None,
                 ticket_id: str | None = None, status: str | None = None) -> list[dict]:
    scope = caller.default_account_scope()
    if scope:
        account_id = scope
    elif account_id:
        caller.assert_can_access_account(account_id)

    conn = _conn()
    if ticket_id:
        rows = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchall()
    elif account_id:
        rows = conn.execute("SELECT * FROM tickets WHERE account_id = ?", (account_id,)).fetchall()
    else:
        if caller.role != "staff":
            raise PermissionError("account_id or ticket_id is required for non-staff callers.")
        rows = conn.execute("SELECT * FROM tickets").fetchall()
    conn.close()

    results = [_row_to_dict(r) for r in rows]
    for r in results:
        caller.assert_can_access_account(r["account_id"])
    if status:
        results = [r for r in results if r["status"] == status]
    return results


def _parse_dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M") if s else None


def calculate_cancellation_fee(caller: CallerContext, order_id: str) -> dict:
    """Deterministic cancellation-fee calculation per SOP v4, with contract
    overrides applied where applicable."""
    orders = get_orders(caller, order_id=order_id)
    if not orders:
        return {"error": f"No order found with id {order_id}"}
    order = orders[0]
    caller.assert_can_access_account(order["account_id"])
    account_id = order["account_id"]
    status = order["status"]
    overrides = CONTRACT_OVERRIDES.get(account_id, {})

    if status == "DRAFT":
        return {"order_id": order_id, "status": status, "cancellable": True,
                "fee_inr": 0, "basis": "SOP v4 1: DRAFT orders cancel free."}
    if status == "PICKED_UP":
        return {"order_id": order_id, "status": status, "cancellable": False,
                "fee_inr": None,
                "basis": "SOP v4 1: PICKED_UP orders cannot be cancelled; use return-to-origin."}
    if status == "DELIVERED":
        return {"order_id": order_id, "status": status, "cancellable": False,
                "fee_inr": None, "basis": "SOP v4 1: DELIVERED orders cannot be cancelled."}

    if status == "BOOKED":
        if overrides.get("cancellation_fee_waived_if_not_picked_up"):
            return {"order_id": order_id, "status": status, "cancellable": True,
                    "fee_inr": 0,
                    "basis": f"Customer agreement for {account_id} waives the cancellation "
                             f"fee for any BOOKED shipment cancelled before pickup, "
                             f"regardless of elapsed time."}
        booked_at = _parse_dt(order["booked_at"])
        cancel_requested_at = _parse_dt(order.get("cancellation_requested_at")) or SNAPSHOT
        if not booked_at:
            return {"error": "Missing booked_at timestamp; cannot calculate."}
        minutes_elapsed = (cancel_requested_at - booked_at).total_seconds() / 60
        if minutes_elapsed <= 30:
            return {"order_id": order_id, "status": status, "cancellable": True,
                    "fee_inr": 0, "minutes_since_booking": round(minutes_elapsed, 1),
                    "basis": "SOP v4 1: cancellation requested within 30 minutes of booking, no fee."}
        return {"order_id": order_id, "status": status, "cancellable": True,
                "fee_inr": 250, "minutes_since_booking": round(minutes_elapsed, 1),
                "basis": "SOP v4 1: cancellation requested more than 30 minutes after "
                         "booking; default INR 250 fee applies (no waiver on file)."}

    return {"error": f"Unrecognized order status '{status}'."}


def calculate_service_credit(caller: CallerContext, order_id: str) -> dict:
    """Deterministic failed-pickup service-credit calculation, with contract
    overrides applied where applicable."""
    orders = get_orders(caller, order_id=order_id)
    if not orders:
        return {"error": f"No order found with id {order_id}"}
    order = orders[0]
    caller.assert_can_access_account(order["account_id"])
    account_id = order["account_id"]
    overrides = CONTRACT_OVERRIDES.get(account_id, {})

    carrier_fault = order["carrier_fault"] in ("True", "1", True)
    customer_fault = order["customer_fault"] in ("True", "1", True)
    window_end = _parse_dt(order["pickup_window_end"])
    pickup_actual = _parse_dt(order.get("pickup_actual_at"))

    if order.get("pickup_actual_at") in (None, "", "None"):
        # Not yet picked up as of the dataset snapshot -- use snapshot time to
        # judge elapsed delay, but flag that this can still change.
        reference_time = SNAPSHOT
        pickup_confirmed = False
    else:
        reference_time = pickup_actual
        pickup_confirmed = True

    if window_end is None:
        return {"error": "Missing pickup_window_end; cannot calculate."}

    hours_late = (reference_time - window_end).total_seconds() / 3600

    if carrier_fault is None or customer_fault is None:
        return {"order_id": order_id, "eligible": "unknown",
                "basis": "SOP v4 3: carrier/customer fault is undetermined; do not "
                         "promise a credit until verified."}

    if customer_fault:
        return {"order_id": order_id, "eligible": False, "hours_late": round(hours_late, 2),
                "basis": "SOP v4 2: customer-caused issue disqualifies the pickup from a "
                         "service credit."}
    if not carrier_fault:
        return {"order_id": order_id, "eligible": False, "hours_late": round(hours_late, 2),
                "basis": "SOP v4 2: carrier is not marked at fault; no service credit applies."}

    threshold_hours = overrides.get("failed_pickup_credit_threshold_hours", 2)
    fixed_amount = overrides.get("failed_pickup_credit_amount_inr")

    if hours_late < threshold_hours:
        return {"order_id": order_id, "eligible": False, "hours_late": round(hours_late, 2),
                "threshold_hours": threshold_hours, "pickup_confirmed": pickup_confirmed,
                "basis": f"Delay is under the {threshold_hours}-hour threshold "
                         f"({'contract override' if account_id in CONTRACT_OVERRIDES else 'SOP v4 default'})."}

    if fixed_amount is not None:
        credit = fixed_amount
        basis = (f"Customer agreement for {account_id} sets a fixed INR {fixed_amount} "
                 f"credit once the pickup is more than {threshold_hours} hours late "
                 f"with carrier at fault.")
    else:
        fee = float(order["shipment_fee_inr"])
        credit = min(500, round(0.10 * fee))
        basis = ("SOP v4 2: default credit is the lower of INR 500 or 10% of the "
                 f"shipment fee (10% of {fee} = {round(0.10 * fee)}).")

    needs_approval = credit > 1000
    return {
        "order_id": order_id, "eligible": True, "credit_inr": credit,
        "hours_late": round(hours_late, 2), "threshold_hours": threshold_hours,
        "pickup_confirmed": pickup_confirmed,
        "requires_manager_approval": needs_approval,
        "basis": basis + (" Requires manager approval (SOP v4 3: credits above INR 1,000)."
                           if needs_approval else ""),
    }
