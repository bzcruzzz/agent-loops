import os

# Model
MODEL = os.environ.get("MINILOOP_MODEL", "claude-sonnet-4-5")

# Anthropic
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Per-token prices (USD) for cost tracking — claude-sonnet-4-5
INPUT_COST_PER_TOKEN  = 3.00 / 1_000_000   # $3.00 / 1M input tokens
OUTPUT_COST_PER_TOKEN = 15.00 / 1_000_000  # $15.00 / 1M output tokens

# Defaults
DEFAULT_MAX_TURNS  = 40
DEFAULT_MAX_BUDGET = 5.0
DB_PATH = "miniloop.db"
WORKSPACES_DIR = "workspaces"
