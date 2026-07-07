# miniloop

Autonomous agent loop engine for IBM Bob — no external API key required.

Bob's own model drives the loop. miniloop handles session state, workspace
execution, task ledger, criteria gate, checkpoints, and audit trail via MCP.

## How it works

```
Bob (model already running)
    │
    ├── loop_start(goal, workspace)   → create session, get instructions
    ├── loop_tool(id, "list_files")   → see workspace
    ├── loop_tool(id, "bash", ...)    → run tests, see failures
    ├── loop_tool(id, "edit_file",..) → apply fix
    ├── loop_tool(id, "bash", ...)    → verify fix passes
    └── loop_finish(id, summary)      → criteria gate → done ✅
```

miniloop makes zero LLM calls. Bob is the model. miniloop is the execution sandbox.

## Setup

```bash
git clone git@github.com:bzcruzzz/agent-loops.git
cd agent-loops
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Register the MCP server in `~/.bob/settings/mcp_settings.json`:

```json
{
  "mcpServers": {
    "miniloop": {
      "type": "stdio",
      "command": "/path/to/agent-loops/.venv/bin/python",
      "args": ["-m", "miniloop.mcp_server"],
      "cwd": "/path/to/agent-loops"
    }
  }
}
```

Then reload MCP servers in Bob (`Cmd+Shift+P` → **MCP: Reload Servers**).

## Usage in Bob chat

```
Set up a workspace with a broken test:
  mkdir -p /tmp/buggy
  echo 'def add(a,b): return a-b' > /tmp/buggy/calc.py
  echo 'from calc import add\ndef test_add(): assert add(2,3)==5' > /tmp/buggy/test_calc.py

Now use miniloop MCP tools to fix it:
  goal: "Fix the failing pytest test"
  workspace: "/tmp/buggy"
```

Bob will call `loop_start`, then iterate with `loop_tool`, then `loop_finish`.

## MCP tools

| Tool | Description |
|---|---|
| `loop_start(goal, workspace?)` | Create session, returns `session_id` + agent instructions |
| `loop_tool(session_id, tool, args)` | Execute one workspace tool: `bash`, `read_file`, `write_file`, `edit_file`, `list_files`, `update_ledger` |
| `loop_finish(session_id, summary)` | Run criteria gate — rejects if any `check_cmd` fails |
| `loop_status(session_id)` | Get task ledger + criteria status |
| `loop_replay(session_id)` | Full event log (audit trail) |

## What miniloop manages

- **Session DB** — SQLite ledger of every session, turn, tool call, and result
- **Task ledger** — planner generates tasks + machine-checkable success criteria at start
- **Criteria gate** — `loop_finish` is rejected until all `check_cmd`s exit 0
- **Checkpoints** — workspace state saved every turn; sessions are resumable
- **Audit trail** — full event log queryable via `loop_replay`

## Architecture

```
miniloop/
├── mcp_server.py   MCP server — the Bob interface (start here)
├── tools/          Workspace execution: bash, read_file, write_file, edit_file, list_files
├── db.py           SQLite session store (7 tables)
├── loop.py         Standalone loop engine (CLI mode, requires LLM key)
└── config.py       Environment config
```

`loop.py` and `__main__.py` are the original standalone CLI — still works if you
configure an LLM backend in `.env`, but the primary interface is the MCP server.
