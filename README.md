# miniloop

Autonomous agent loop SDK for IBM Bob.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure your LLM backend in .env (see .env.example)
cp .env.example .env
# edit .env with your API_KEY, API_BASE_URL, MINILOOP_MODEL

python -m miniloop "Build a Flask app with /health endpoint and passing pytest tests" \
  --autonomy auto --max-budget 3.0
```

## Demo commands

```bash
# Self-correction test (CP2)
mkdir -p /tmp/buggy
printf 'def add(a,b):\n    return a-b\n' > /tmp/buggy/calc.py
printf 'from calc import add\ndef test_add():\n    assert add(2,3)==5\n' > /tmp/buggy/test_calc.py
python -m miniloop "fix the failing test" --workspace /tmp/buggy --autonomy auto

# Resume a suspended session
python -m miniloop --resume <session_id> --max-turns +20

# Audit trail replay
python -m miniloop --replay <session_id>
```

## Architecture

1. **Loop** — model → tool calls → results → repeat until `finish()` or limit hit
2. **Ledger** — versioned task + criteria plan; machine-checkable success criteria required
3. **Criteria gate** — `finish()` is rejected unless all `check_cmd`s exit 0
4. **Approvals** — `bash` prompts y/a/n in supervised mode; `--autonomy auto` skips
5. **Checkpoints** — full message log saved every turn; `--resume` restores exactly
6. **Critic** — separate fresh-context model call every 6 turns; injects redirect if off-track

## Configuration

| Variable | Default | Description |
|---|---|---|
| `API_BASE_URL` | Bob API | Any OpenAI-compatible endpoint |
| `API_KEY` | — | Your API key |
| `API_AUTH_SCHEME` | `apikey` | `apikey` or `Bearer` |
| `MINILOOP_MODEL` | `claude-sonnet-4-5` | Model name |

Works with Bob API, Anthropic, OpenAI, Ollama, LiteLLM proxy — anything OpenAI-compatible.
