from pathlib import Path
from dotenv import load_dotenv
import os

# Always let the .env file in backend/ win over any stray system env var.
BASE_DIR = Path(__file__).resolve().parent.parent
# load_dotenv with encoding="utf-8-sig" strips a UTF-8 byte-order-mark (BOM)
# if present. This matters because PowerShell's `Out-File -Encoding utf8`
# silently prepends a BOM, which otherwise corrupts the *first* key in the
# file (e.g. "GEMINI_API_KEY" is read as "\ufeffGEMINI_API_KEY" and never
# matches os.environ.get("GEMINI_API_KEY")). Safe for plain UTF-8 files too.
load_dotenv(BASE_DIR / ".env", override=True, encoding="utf-8-sig")

# Gemini (Google AI Studio) is the only LLM backend. It exposes an
# OpenAI-compatible /chat/completions endpoint with function calling, so the
# agent loop and tool schemas are plain OpenAI-style.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

LLM_MODEL = GEMINI_MODEL  # kept as a name for anything that still refers to LLM_MODEL

DATA_DIR = BASE_DIR / "data"
DOCS_DIR = DATA_DIR / "docs"
INDEX_DIR = BASE_DIR / "index"
DB_PATH = BASE_DIR / "parcelpilot.db"
DOC_REGISTRY_PATH = DATA_DIR / "doc_registry.json"

# Reference "now" for all time-based reasoning, per the dataset README.
DATASET_SNAPSHOT = "2026-08-16 11:00"
