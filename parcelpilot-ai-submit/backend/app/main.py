from pathlib import Path

import traceback

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.agent import run_agent
from app.auth import CallerContext
from app.tools import actions
from app.insights import build_insights
import sqlite3
from app.config import DB_PATH

app = FastAPI(title="ParcelPilot AI Support")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Guarantees the frontend always gets JSON back, never an HTML error page
    # that breaks `res.json()` client-side (e.g. plain uvicorn/FastAPI 500s).
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"reply": f"Server error: {exc}", "trace": [],
                 "trust": {"level": "error", "reasons": ["Unhandled server exception."]}},
    )


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    role: str            # "customer" | "staff"
    account_id: str | None = None
    staff_name: str | None = None


@app.post("/api/chat")
def chat(req: ChatRequest):
    if req.role not in ("customer", "staff"):
        raise HTTPException(400, "role must be 'customer' or 'staff'")
    if req.role == "customer" and not req.account_id:
        raise HTTPException(400, "account_id is required for customer role")

    caller = CallerContext(role=req.role, account_id=req.account_id, staff_name=req.staff_name)
    result = run_agent([m.model_dump() for m in req.messages], caller)
    return result


@app.post("/api/actions/{action_id}/confirm")
def confirm_action(action_id: str):
    result = actions.execute_action(action_id)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.get("/api/actions/{action_id}")
def get_action(action_id: str):
    return actions.get_action(action_id)


@app.get("/api/insights")
def insights():
    return build_insights()


@app.get("/api/accounts")
def list_accounts():
    """Lets the demo frontend populate an account picker without hardcoding IDs."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT account_id, account_name, plan FROM accounts").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- serve the static frontend (so a single deploy works) ---
FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def root():
        return FileResponse(FRONTEND_DIR / "index.html")
