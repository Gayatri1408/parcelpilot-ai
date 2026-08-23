"""
Mocked authentication. In a real system this would come from a verified
session/JWT. Here the frontend sends a role + account_id, and every data/tool
call is scoped against it server-side -- the LLM never decides access, it only
ever sees data this layer already filtered.
"""
from dataclasses import dataclass
from fastapi import HTTPException


@dataclass
class CallerContext:
    role: str          # "customer" | "staff"
    account_id: str | None = None   # required for role == "customer"
    staff_name: str | None = None   # display name for role == "staff"

    def assert_can_access_account(self, target_account_id: str):
        if self.role == "customer" and target_account_id != self.account_id:
            raise HTTPException(
                status_code=403,
                detail="Customers may only access their own account's data.",
            )
        # staff: unrestricted in this assessment scope (see architecture note
        # for how per-role staff scoping would be layered on top).

    def default_account_scope(self) -> str | None:
        """If the caller is a customer, every unscoped query is implicitly
        limited to their own account."""
        return self.account_id if self.role == "customer" else None
