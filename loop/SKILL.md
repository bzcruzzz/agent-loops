---
name: loop
description: >-
  Run an autonomous agent loop using miniloop MCP tools. Bob's model drives
  the loop — no external API key needed. Handles planning, tool execution,
  criteria gate, checkpoints, and audit trail.
  Trigger phrases: "/loop", "run the loop", "use miniloop", "autonomous agent",
  "fix it autonomously", "loop start", "use the loop tools".
metadata:
  author: miniloop
  version: "1.0"
  display_name: /loop
  short_description: Autonomous agent loop — Bob drives the loop via miniloop MCP tools. No API key needed.
  iconName: play
  example_prompts:
    - "/loop fix the failing tests in /tmp/buggy"
    - "/loop build a fizzbuzz function with passing pytest tests"
    - "Use miniloop to fix the bug in my project"
    - "Run the loop on this workspace"
---

# /loop Skill

You are running an autonomous agent loop using the miniloop MCP tools.
Bob's own model (you) drives every turn — miniloop manages session state,
workspace execution, criteria gate, and audit trail.

## Mandatory workflow — follow this exactly

### Step 1 — Start the session
Call `loop_start` with the goal and workspace path.
- If the user didn't provide a workspace, ask for one before proceeding.
- The response contains `session_id`, `workspace`, `tasks`, `criteria`, and `instructions`.
- Read the `instructions` field — it is your operating procedure for this session.

### Step 2 — Investigate
Call `loop_tool` with `tool="list_files"` to see the workspace structure.
Call `loop_tool` with `tool="bash"` and `command="python3 -m pytest -v"` to run
tests and see what's failing. Never assume file contents — always read first.

### Step 3 — Loop until done
For each turn:
1. Decide the next action based on what you know
2. Call `loop_tool` with the appropriate tool and args
3. Read the result — `ok`, `output`, `error`, `ledger_summary`
4. Update the ledger via `loop_tool("update_ledger")` as tasks complete
5. Repeat until all tests pass and success criteria are met

### Step 4 — Finish
Only call `loop_finish` after running tests and confirming they pass.
- If `loop_finish` returns `passed: false`, read the `failing` list and fix each item
- Keep looping until `loop_finish` returns `passed: true`

### Step 5 — Report
When `loop_finish` passes, report to the user:
- What was fixed
- How many turns it took
- Offer to call `loop_replay` for the full audit trail

---

## Available tools

| Tool | When to use |
|---|---|
| `loop_start(goal, workspace)` | Always first |
| `loop_tool(session_id, "list_files", {})` | See workspace structure |
| `loop_tool(session_id, "bash", {"command": "..."})` | Run commands, tests |
| `loop_tool(session_id, "read_file", {"path": "..."})` | Read a file |
| `loop_tool(session_id, "write_file", {"path": "...", "content": "..."})` | Create a file |
| `loop_tool(session_id, "edit_file", {"path": "...", "old_str": "...", "new_str": "..."})` | Edit a file |
| `loop_tool(session_id, "update_ledger", {"tasks": [...], "criteria": [...]})` | Update task/criteria status |
| `loop_finish(session_id, summary)` | Signal done — runs criteria gate |
| `loop_status(session_id)` | Re-orient mid-session |
| `loop_replay(session_id)` | Get full audit trail |

---

## Rules

- **Never guess file contents** — always `read_file` before `edit_file`
- **Always run tests before `loop_finish`** — it will be rejected if they fail
- **Use `edit_file` for targeted changes**, `write_file` only for new files
- **If stuck**, call `loop_status` to re-read the ledger and reorient
- **One tool call per turn** — don't try to batch multiple actions in one `loop_tool` call
