# miniloop

Autonomous agent loop engine for IBM Bob — no external API key required.

Bob's own model drives the loop via a `/loop` slash command. miniloop handles
session state, workspace execution, task ledger, criteria gate, checkpoints,
and audit trail via MCP.

## How it works

```
You type: /loop fix the bug in /tmp/demo
               │
               ▼
        Bob activates the loop skill
        (loads workflow instructions)
               │
               ▼
        Bob calls miniloop MCP tools turn by turn:
               │
               ├── loop_start(goal, workspace)
               ├── loop_tool(id, "list_files", {})
               ├── loop_tool(id, "bash", {"command": "pytest -v"})
               ├── loop_tool(id, "read_file", {"path": "..."})
               ├── loop_tool(id, "edit_file", {...})
               ├── loop_tool(id, "bash", {"command": "pytest -v"})
               └── loop_finish(id, summary) → criteria gate → done ✅
```

The skill is the trigger + instructions. The MCP server is the execution engine.
Neither works without the other. miniloop makes zero LLM calls — Bob is the model.

## Setup

```bash
git clone git@github.com:bzcruzzz/agent-loops.git
cd agent-loops
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 1 — Register the MCP server

Add to `~/.bob/settings/mcp_settings.json`:

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

Reload: `Cmd+Shift+P` → **MCP: Reload Servers**

### 2 — Install the skill

```bash
cp -r loop ~/.bob/skills/loop
```

Bob picks it up automatically — no restart needed.

## Usage

```
/loop fix the failing tests in /tmp/buggy
```

```
/loop build a fizzbuzz function with passing pytest tests
workspace: /tmp/fizz
```

Bob calls `loop_start`, iterates with `loop_tool`, then `loop_finish`. The
criteria gate runs the test suite independently — `loop_finish` is rejected
until all `check_cmd`s exit 0.

## MCP tools

| Tool | Description |
|---|---|
| `loop_start(goal, workspace?)` | Create session, return `session_id` + agent instructions |
| `loop_tool(session_id, tool, args)` | Execute one workspace tool: `bash`, `read_file`, `write_file`, `edit_file`, `list_files`, `update_ledger` |
| `loop_finish(session_id, summary)` | Run criteria gate — rejects if any `check_cmd` fails |
| `loop_status(session_id)` | Get task ledger + criteria status |
| `loop_replay(session_id)` | Full event log (audit trail) |

## What miniloop manages

- **Session DB** — SQLite ledger of every session, turn, tool call, and result
- **Task ledger** — planner generates tasks + machine-checkable success criteria at session start
- **Criteria gate** — `loop_finish` is rejected until all `check_cmd`s exit 0
- **Checkpoints** — workspace state saved every turn; sessions are resumable
- **Audit trail** — full event log queryable via `loop_replay`

## Architecture

```
miniloop/
├── mcp_server.py   MCP server — loop_start / loop_tool / loop_finish / loop_status / loop_replay
├── tools/          Workspace execution: bash, read_file, write_file, edit_file, list_files
├── db.py           SQLite session store (7 tables)
├── loop.py         Standalone CLI loop engine (requires LLM key in .env)
└── config.py       Environment config

loop/
└── SKILL.md        Bob skill — activates on /loop, instructs Bob how to drive the MCP tools
```

## Relationship between skill and MCP

The `/loop` command needs both:

- **Skill** (`loop/SKILL.md`) — the trigger and workflow instructions. Tells Bob to call `loop_start` first, iterate with `loop_tool`, enforce `loop_finish` only when tests pass.
- **MCP server** (`miniloop/mcp_server.py`) — the execution engine. Actually runs bash, reads/writes files, enforces the criteria gate, saves checkpoints to SQLite.

Skill without MCP → Bob has instructions but no tools to call.
MCP without skill → Bob has tools but doesn't know how to chain them.
