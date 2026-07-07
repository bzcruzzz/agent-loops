import os
from pathlib import Path

# Load .env from project root if present
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass

# ── LLM backend (bring your own) ─────────────────────────────────────────────
# Set these in .env — miniloop works with any OpenAI-compatible endpoint.
#
# Examples:
#   Bob API:      API_BASE_URL=https://api.us-east.bob.ibm.com/inference/v1
#                 API_KEY=<bob inference key>
#                 API_AUTH_SCHEME=apikey          (default: Bearer)
#
#   Anthropic:    API_BASE_URL=https://api.anthropic.com/v1
#                 API_KEY=<anthropic key>
#
#   Ollama:       API_BASE_URL=http://localhost:11434/v1
#                 API_KEY=ollama
#
#   OpenAI:       API_BASE_URL=https://api.openai.com/v1
#                 API_KEY=<openai key>

API_BASE_URL    = os.environ.get("API_BASE_URL", "https://api.us-east.bob.ibm.com/inference/v1")
API_KEY         = os.environ.get("API_KEY") or os.environ.get("BOB_API_KEY", "")
API_AUTH_SCHEME = os.environ.get("API_AUTH_SCHEME", "apikey")   # "Bearer" or "apikey"
MODEL           = os.environ.get("MINILOOP_MODEL", "claude-sonnet-4-5")

# Per-token prices (USD) for cost tracking
INPUT_COST_PER_TOKEN  = float(os.environ.get("INPUT_COST_PER_TOKEN",  "0.000003"))
OUTPUT_COST_PER_TOKEN = float(os.environ.get("OUTPUT_COST_PER_TOKEN", "0.000015"))

# Defaults
DEFAULT_MAX_TURNS  = 40
DEFAULT_MAX_BUDGET = 5.0
DB_PATH            = "miniloop.db"
WORKSPACES_DIR     = "workspaces"
