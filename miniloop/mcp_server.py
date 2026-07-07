"""
miniloop MCP server — Bob-native zero-key architecture.

How it works
------------
Bob's own model IS the agent loop. miniloop exposes the workspace execution
primitives and session state as MCP tools. Bob calls them turn by turn —
no external API key, no subprocess, no LLM calls from miniloop at all.

  Bob (model already running)
      │
      ├── loop_start(goal)          create session, return session_id + system prompt
      ├── loop_tool(id, tool, args) execute one workspace tool, return result + updated ledger
      ├── loop_finish(id, summary)  run criteria gate → pass or return failing criteria
      ├── loop_status(id)           get ledger / stats mid-session
      └── loop_replay(id)           get full event log

Bob's system prompt instructs it to:
  1. Call loop_start → get session_id and instructions
  2. Loop: call loop_tool (bash/read_file/etc.) → read result → decide → repeat
  3. When done: call loop_finish → if criteria fail, keep going; if pass, done

This is exactly how Bob's own agent mode works — just with miniloop managing
the session DB, ledger, criteria gate, checkpoints, and audit trail.

Register in ~/.bob/settings/mcp_settings.json:
  {
    "mcpServers": {
      "miniloop": {
        "type": "stdio",
        "command": "/Users/bradencruz/Projects/agent-loops/.venv/bin/python",
        "args": ["-m", "miniloop.mcp_server"],
        "cwd": "/Users/bradencruz/Projects/agent-loops"
      }
    }
  }

Then in Bob chat:
  Use the miniloop MCP tools to autonomously complete this goal:
  "Fix the failing pytest tests in /tmp/buggy"
"""
import argparse
import json
import os
import subprocess
import sys
import time
import uuid

from miniloop.db import (
    init_db, insert_session, get_session, update_session,
    get_tasks, get_criteria, get_events,
    insert_event, insert_checkpoint, max_event_seq,
    upsert_task, upsert_criterion,
)
from miniloop import config
from miniloop.tools import dispatch, EFFECT

# ── system prompt Bob gets when it calls loop_start ──────────────────────────

AGENT_SYSTEM_PROMPT = """\
You are an autonomous software engineer. You have been given a goal and a \
set of miniloop MCP tools to complete it.

WORKFLOW — follow exactly:
1. You already called loop_start and have a session_id.
2. Call loop_tool with tool="list_files" to see the workspace.
3. Call loop_tool with tool="bash", args={"command":"python3 -m pytest -v"} \
to run tests and see failures.
4. Call loop_tool with tool="read_file" to read files you need to fix.
5. Call loop_tool with tool="edit_file" or "write_file" to apply fixes.
6. Call loop_tool with tool="bash" again to verify fixes pass.
7. Only call loop_finish AFTER tests pass. It will be REJECTED if criteria fail.

RULES:
- Always use loop_tool — never assume file contents without reading.
- Always run tests before calling loop_finish.
- If loop_finish is rejected, read the failing criteria and fix them.
- Call loop_status any time you need to re-orient on the ledger."""

# ── tool definitions ──────────────────────────────────────────────────────────

TOOL_DEFS = [
    {
        "name": "loop_start",
        "description": (
            "Start a new autonomous agent session. Returns the session_id and "
            "the system instructions you must follow to complete the goal. "
            "Call this FIRST before any loop_tool calls."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "The goal to accomplish"
                },
                "workspace": {
                    "type": "string",
                    "description": "Absolute path to the working directory. "
                                   "A fresh temp dir is created if omitted."
                },
                "max_turns": {
                    "type": "integer",
                    "description": f"Max tool calls before auto-suspend (default {config.DEFAULT_MAX_TURNS})"
                },
                "max_budget": {
                    "type": "number",
                    "description": f"Spend cap USD — always 0 in Bob-native mode (default {config.DEFAULT_MAX_BUDGET})"
                },
            },
            "required": ["goal"],
        },
    },
    {
        "name": "loop_tool",
        "description": (
            "Execute one workspace tool inside the agent session. "
            "Available tools: bash, read_file, write_file, edit_file, list_files, update_ledger. "
            "Returns {ok, output, error, turn, ledger_summary}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID from loop_start"
                },
                "tool": {
                    "type": "string",
                    "enum": ["bash", "read_file", "write_file", "edit_file",
                             "list_files", "update_ledger"],
                    "description": "Tool to execute"
                },
                "args": {
                    "type": "object",
                    "description": (
                        "Tool arguments. "
                        "bash: {command}. "
                        "read_file: {path}. "
                        "write_file: {path, content}. "
                        "edit_file: {path, old_str, new_str}. "
                        "list_files: {pattern?}. "
                        "update_ledger: {tasks?, criteria?}."
                    )
                },
            },
            "required": ["session_id", "tool", "args"],
        },
    },
    {
        "name": "loop_finish",
        "description": (
            "Signal that the goal is complete. miniloop will verify all success criteria "
            "by running their check commands. Returns {passed: true} on success, or "
            "{passed: false, failing: [...]} if criteria still fail — in which case "
            "you must fix them and call loop_finish again."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "summary": {
                    "type": "string",
                    "description": "Short summary of what was accomplished"
                },
            },
            "required": ["session_id", "summary"],
        },
    },
    {
        "name": "loop_status",
        "description": "Get current session status, task ledger, and success criteria.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"}
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "loop_replay",
        "description": "Return the full event log for a session (audit trail).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"}
            },
            "required": ["session_id"],
        },
    },
]

# ── tool handlers ─────────────────────────────────────────────────────────────

def handle_loop_start(args: dict) -> dict:
    goal       = args["goal"]
    max_turns  = int(args.get("max_turns",  config.DEFAULT_MAX_TURNS))
    max_budget = float(args.get("max_budget", config.DEFAULT_MAX_BUDGET))

    session_id = str(uuid.uuid4())
    workspace  = args.get("workspace") or os.path.abspath(
        os.path.join(config.WORKSPACES_DIR, session_id)
    )
    os.makedirs(workspace, exist_ok=True)

    insert_session(
        session_id=session_id,
        goal=goal,
        workspace_path=os.path.abspath(workspace),
        max_turns=max_turns,
        max_budget_usd=max_budget,
        model="bob-native",
    )
    update_session(session_id, status="running")
    insert_event(session_id, 0, "system", {"event": "session_start", "goal": goal})

    # Run the planner to generate tasks + criteria
    _run_planner(session_id, goal, workspace)

    tasks    = [dict(t) for t in get_tasks(session_id)]
    criteria = [dict(c) for c in get_criteria(session_id)]

    return {
        "session_id": session_id,
        "workspace":  workspace,
        "goal":       goal,
        "tasks":      tasks,
        "criteria":   criteria,
        "instructions": AGENT_SYSTEM_PROMPT,
        "message": (
            f"Session {session_id[:8]} started. Workspace: {workspace}. "
            f"Follow the instructions field exactly. "
            f"Use loop_tool to execute tools, loop_finish when done."
        ),
    }


def handle_loop_tool(args: dict) -> dict:
    session_id = args["session_id"]
    tool_name  = args["tool"]
    tool_args  = args.get("args", {})

    row = get_session(session_id)
    if not row:
        return {"ok": False, "error": f"Session {session_id} not found"}

    workspace  = row["workspace_path"]
    turn       = row["turn_count"] + 1

    # Check turn limit
    if turn > row["max_turns"]:
        update_session(session_id, status="suspended")
        insert_event(session_id, turn, "system",
                     {"event": "suspended", "reason": "max_turns"})
        return {
            "ok": False,
            "error": f"Turn limit ({row['max_turns']}) reached. "
                     f"Call loop_status to see progress, then loop_finish if done.",
        }

    # Handle update_ledger specially
    if tool_name == "update_ledger":
        for t in tool_args.get("tasks") or []:
            upsert_task(session_id, t["id"], t["title"],
                        t.get("status", "pending"), t.get("depends_on", []), 1)
        for c in tool_args.get("criteria") or []:
            upsert_criterion(session_id, c["id"], c["description"],
                             c["check_cmd"], c.get("status", "pending"), "")
        update_session(session_id, turn_count=turn)
        insert_event(session_id, turn, "tool",
                     {"tool": "update_ledger", "ok": True})
        return {"ok": True, "output": "ledger updated", "error": None,
                "turn": turn, "ledger_summary": _ledger_summary(session_id)}

    # Execute the tool
    t0     = time.monotonic()
    result = dispatch(tool_name, tool_args, workspace, session_id)
    dur    = int((time.monotonic() - t0) * 1000)

    update_session(session_id, turn_count=turn)
    insert_event(session_id, turn, "tool", {
        "tool": tool_name, "args": tool_args,
        "ok": result["ok"], "duration_ms": dur,
    })
    insert_checkpoint(session_id, turn, max_event_seq(session_id), [])

    return {
        "ok":             result["ok"],
        "output":         result.get("output", ""),
        "error":          result.get("error"),
        "turn":           turn,
        "ledger_summary": _ledger_summary(session_id),
    }


def handle_loop_finish(args: dict) -> dict:
    session_id = args["session_id"]
    summary    = args.get("summary", "")

    row = get_session(session_id)
    if not row:
        return {"passed": False, "error": f"Session {session_id} not found"}

    workspace = row["workspace_path"]
    criteria  = get_criteria(session_id)

    if not criteria:
        # No criteria — accept finish
        update_session(session_id, status="success")
        insert_event(session_id, row["turn_count"], "result",
                     {"status": "success", "summary": summary})
        return {"passed": True, "summary": summary, "session_id": session_id}

    # Run every check_cmd
    failing = []
    for c in criteria:
        try:
            from miniloop.tools import _VENV_BIN
            env = os.environ.copy()
            env["PATH"] = _VENV_BIN + ":" + env.get("PATH", "")
            proc = subprocess.run(
                c["check_cmd"], shell=True, cwd=workspace,
                capture_output=True, text=True, timeout=60, env=env,
            )
            passed = proc.returncode == 0
            output = (proc.stdout + proc.stderr)[:1000]
        except Exception as e:
            passed, output = False, str(e)

        status = "passed" if passed else "failed"
        upsert_criterion(session_id, c["id"], c["description"],
                         c["check_cmd"], status, output)
        if not passed:
            failing.append({
                "id":          c["id"],
                "description": c["description"],
                "check_cmd":   c["check_cmd"],
                "output":      output[-500:],
            })

    if failing:
        insert_event(session_id, row["turn_count"], "eval",
                     {"event": "finish_rejected", "failing": len(failing)})
        return {
            "passed":  False,
            "failing": failing,
            "message": (
                "Criteria not yet passing. Fix the issues above, "
                "then call loop_finish again."
            ),
        }

    update_session(session_id, status="success")
    insert_event(session_id, row["turn_count"], "result",
                 {"status": "success", "summary": summary})
    return {
        "passed":     True,
        "summary":    summary,
        "session_id": session_id,
        "turns":      row["turn_count"],
    }


def handle_loop_status(args: dict) -> dict:
    session_id = args["session_id"]
    row = get_session(session_id)
    if not row:
        return {"error": f"Session {session_id} not found"}
    return {
        "session_id": session_id,
        "status":     row["status"],
        "goal":       row["goal"],
        "turns":      row["turn_count"],
        "max_turns":  row["max_turns"],
        "workspace":  row["workspace_path"],
        "tasks":      [dict(t) for t in get_tasks(session_id)],
        "criteria":   [dict(c) for c in get_criteria(session_id)],
    }


def handle_loop_replay(args: dict) -> dict:
    session_id = args["session_id"]
    events = get_events(session_id)
    if not events:
        return {"error": f"No events for session {session_id}"}
    rows = []
    for ev in events:
        try:
            payload = json.loads(ev["payload"])
        except Exception:
            payload = ev["payload"]
        rows.append({"seq": ev["seq"], "turn": ev["turn"],
                     "type": ev["type"], "payload": payload})
    return {"session_id": session_id, "events": rows}


HANDLERS = {
    "loop_start":  handle_loop_start,
    "loop_tool":   handle_loop_tool,
    "loop_finish": handle_loop_finish,
    "loop_status": handle_loop_status,
    "loop_replay": handle_loop_replay,
}

# ── planner (calls miniloop's internal planner once at session start) ─────────

def _run_planner(session_id: str, goal: str, workspace: str) -> None:
    """
    Generate a plan for the goal using the configured LLM (if available),
    or fall back to a minimal default plan so the session is always usable.
    """
    # Bob is the model — skip external LLM planner, use default plan directly
    upsert_task(session_id, "T1", "Understand the workspace", "pending", [], 1)
    upsert_task(session_id, "T2", "Implement the solution", "pending", ["T1"], 1)
    upsert_task(session_id, "T3", "Verify all tests pass", "pending", ["T2"], 1)
    insert_event(session_id, 0, "plan",
                 {"tasks": 3, "criteria": 0, "source": "default"})


def _ledger_summary(session_id: str) -> str:
    tasks    = get_tasks(session_id)
    criteria = get_criteria(session_id)
    lines    = []
    for t in tasks:
        lines.append(f"[{t['status'].upper()}] {t['id']}: {t['title']}")
    for c in criteria:
        lines.append(f"[{c['status'].upper()}] {c['id']}: {c['description']}")
    return "\n".join(lines) if lines else "(no ledger yet)"


# ── JSON-RPC 2.0 stdio transport ──────────────────────────────────────────────

def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def serve_stdio() -> None:
    # Defer init_db() until first real interaction — Bob times out if the
    # process takes too long to respond to the initialize handshake.
    db_ready = False

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            _send({"jsonrpc":"2.0","id":None,
                   "error":{"code":-32700,"message":"Parse error"}})
            continue

        id_    = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {})

        if method == "initialize":
            # Respond immediately — no DB or file I/O before this
            _send({"jsonrpc":"2.0","id":id_,"result":{
                "protocolVersion": "2024-11-05",
                "capabilities":    {"tools": {}},
                "serverInfo":      {"name": "miniloop", "version": "0.2.0"},
            }})
        elif method in ("notifications/initialized", "notifications/cancelled"):
            if not db_ready:
                init_db()
                db_ready = True
        elif method == "tools/list":
            _send({"jsonrpc":"2.0","id":id_,"result":{"tools": TOOL_DEFS}})
        elif method == "tools/call":
            name    = params.get("name", "")
            handler = HANDLERS.get(name)
            if not handler:
                _send({"jsonrpc":"2.0","id":id_,
                       "error":{"code":-32601,"message":f"Unknown tool: {name}"}})
                continue
            try:
                result = handler(params.get("arguments", {}))
                _send({"jsonrpc":"2.0","id":id_,"result":{
                    "content": [{"type":"text","text":json.dumps(result, indent=2)}],
                    "isError": "error" in result and not result.get("ok", True),
                }})
            except Exception as e:
                _send({"jsonrpc":"2.0","id":id_,
                       "error":{"code":-32603,"message":str(e)}})
        elif id_ is not None:
            _send({"jsonrpc":"2.0","id":id_,
                   "error":{"code":-32601,"message":f"Unknown method: {method}"}})


def main():
    parser = argparse.ArgumentParser(prog="miniloop-mcp")
    parser.add_argument("--port", type=int, default=None,
                        help="Serve SSE on this port for debugging")
    a = parser.parse_args()
    if a.port:
        _serve_sse(a.port)
    else:
        serve_stdio()


def _serve_sse(port: int) -> None:
    try:
        from flask import Flask, Response, request as freq
    except ImportError:
        print("SSE mode needs flask: pip install flask", file=sys.stderr)
        sys.exit(1)
    app = Flask(__name__)
    init_db()

    @app.route("/sse")
    def sse():
        return Response(
            (f'data: {{"type":"endpoint","endpoint":"/message"}}\n\n',),
            mimetype="text/event-stream",
        )

    @app.route("/message", methods=["POST"])
    def message():
        req    = freq.get_json(force=True)
        params = req.get("params", {})
        if req.get("method") == "tools/call":
            name    = params.get("name", "")
            handler = HANDLERS.get(name)
            if handler:
                result = handler(params.get("arguments", {}))
                return {"content": [{"type":"text",
                                     "text":json.dumps(result, indent=2)}]}
        return {"error": "unsupported"}

    print(f"miniloop MCP SSE on http://localhost:{port}/sse", file=sys.stderr)
    app.run(port=port)


if __name__ == "__main__":
    main()
