"""
miniloop MCP server — exposes the loop engine as MCP tools so Bob (or any
MCP-compatible host) can drive autonomous agent sessions without an external
API key.

Architecture
------------
When Bob calls loop_start("fix the failing tests"), miniloop does NOT make
its own LLM calls. Instead the loop runs in "headless tool execution" mode:

  Bob (the model) ──calls──► loop_start MCP tool
                                  │
                                  ▼
                        miniloop spawns a subprocess:
                          python -m miniloop "<goal>" --autonomy auto
                                  │
                          (subprocess uses whatever .env says for LLM)
                                  │
                          returns when done/suspended
                                  ▼
                        MCP returns {session_id, status, summary}

This means:
  - If .env has a Bob API key  → uses that
  - If .env has no key at all  → subprocess fails gracefully with a message
  - The MCP server itself is always available regardless

The REAL zero-key path (Phase 2 of this work):
  Replace the subprocess with a callback that calls back into Bob's own
  model via the MCP reverse-call protocol (tools/call on the host).
  That's ~1 day of additional work once this scaffold is proven.

Tools exposed:
  loop_start    — start a new session
  loop_status   — get ledger + stats
  loop_resume   — resume a suspended session
  loop_replay   — get full event log

Register in ~/.bob/settings/mcp_settings.json:
  {
    "mcpServers": {
      "miniloop": {
        "type": "stdio",
        "command": "/absolute/path/to/.venv/bin/python",
        "args": ["-m", "miniloop.mcp_server"],
        "cwd": "/absolute/path/to/agent-loops"
      }
    }
  }
"""
import json
import os
import subprocess
import sys
import uuid
import argparse

from miniloop.db import (
    init_db, insert_session, get_session,
    get_tasks, get_criteria, get_events,
)
from miniloop import config

# ── tool definitions (MCP schema) ─────────────────────────────────────────────

TOOL_DEFS = [
    {
        "name": "loop_start",
        "description": (
            "Start an autonomous agent loop for a goal. The agent will plan, "
            "write and run code, fix failing tests, and iterate until all "
            "success criteria pass. Returns the session ID and final status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "What the agent should accomplish"
                },
                "workspace": {
                    "type": "string",
                    "description": "Absolute path to working directory. A temp dir is created if omitted."
                },
                "max_turns": {
                    "type": "integer",
                    "description": f"Turn limit (default {config.DEFAULT_MAX_TURNS})"
                },
                "max_budget": {
                    "type": "number",
                    "description": f"Spend cap in USD (default {config.DEFAULT_MAX_BUDGET})"
                },
            },
            "required": ["goal"],
        },
    },
    {
        "name": "loop_status",
        "description": "Get status, task ledger, and success criteria for a session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"}
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "loop_resume",
        "description": "Resume a suspended session from its latest checkpoint.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "max_turns":  {"type": "string",
                               "description": "'+N' to extend by N, or plain number to set absolute"},
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

# ── helpers ───────────────────────────────────────────────────────────────────

def _python() -> str:
    """Absolute path to the venv python that has miniloop installed."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    venv_py = os.path.join(here, ".venv", "bin", "python")
    return venv_py if os.path.exists(venv_py) else sys.executable


def _run_miniloop(args_list: list) -> tuple[int, str]:
    """Run `python -m miniloop <args>` as a subprocess, return (returncode, output)."""
    cmd = [_python(), "-m", "miniloop"] + args_list
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run(
        cmd, cwd=here,
        capture_output=True, text=True, timeout=3600,   # 1h hard cap
    )
    return proc.returncode, (proc.stdout + proc.stderr)


# ── tool handlers ─────────────────────────────────────────────────────────────

def handle_loop_start(args: dict) -> dict:
    goal       = args["goal"]
    workspace  = args.get("workspace") or ""
    max_turns  = str(args.get("max_turns",  config.DEFAULT_MAX_TURNS))
    max_budget = str(args.get("max_budget", config.DEFAULT_MAX_BUDGET))

    cli_args = [goal, "--autonomy", "auto",
                "--max-turns", max_turns, "--max-budget", max_budget]
    if workspace:
        cli_args += ["--workspace", workspace]

    # Capture the session_id printed on line 1 ("session: <uuid>")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd  = [_python(), "-m", "miniloop"] + cli_args
    proc = subprocess.run(cmd, cwd=here, capture_output=True, text=True, timeout=3600)
    output = proc.stdout + proc.stderr

    # Parse session_id from first line
    session_id = None
    for line in output.splitlines():
        if line.startswith("session: "):
            session_id = line.split("session: ", 1)[1].strip()
            break

    if not session_id:
        return {"error": "miniloop subprocess failed to start", "output": output[:2000]}

    row = get_session(session_id)
    return {
        "session_id": session_id,
        "status":     row["status"] if row else "unknown",
        "turns":      row["turn_count"] if row else 0,
        "cost_usd":   round(row["cost_usd"], 4) if row else 0,
        "workspace":  row["workspace_path"] if row else "",
        "output_tail": output[-2000:],
    }


def handle_loop_status(args: dict) -> dict:
    session_id = args["session_id"]
    row = get_session(session_id)
    if not row:
        return {"error": f"Session {session_id} not found"}
    tasks    = [dict(t) for t in get_tasks(session_id)]
    criteria = [dict(c) for c in get_criteria(session_id)]
    return {
        "session_id": session_id,
        "status":     row["status"],
        "goal":       row["goal"],
        "turns":      row["turn_count"],
        "max_turns":  row["max_turns"],
        "cost_usd":   round(row["cost_usd"], 4),
        "workspace":  row["workspace_path"],
        "tasks":      tasks,
        "criteria":   criteria,
    }


def handle_loop_resume(args: dict) -> dict:
    session_id = args["session_id"]
    row = get_session(session_id)
    if not row:
        return {"error": f"Session {session_id} not found"}

    cli_args = ["--resume", session_id, "--autonomy", "auto"]
    if "max_turns" in args:
        cli_args += ["--max-turns", str(args["max_turns"])]

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run(
        [_python(), "-m", "miniloop"] + cli_args,
        cwd=here, capture_output=True, text=True, timeout=3600,
    )
    updated = get_session(session_id)
    return {
        "session_id": session_id,
        "status":     updated["status"] if updated else "unknown",
        "turns":      updated["turn_count"] if updated else 0,
        "cost_usd":   round(updated["cost_usd"], 4) if updated else 0,
        "output_tail": (proc.stdout + proc.stderr)[-2000:],
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
    "loop_status": handle_loop_status,
    "loop_resume": handle_loop_resume,
    "loop_replay": handle_loop_replay,
}

# ── JSON-RPC 2.0 stdio transport ──────────────────────────────────────────────

def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def serve_stdio() -> None:
    init_db()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            _send({"jsonrpc":"2.0","id":None,"error":{"code":-32700,"message":"Parse error"}})
            continue

        id_    = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {})

        if method == "initialize":
            _send({"jsonrpc":"2.0","id":id_,"result":{
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "miniloop", "version": "0.1.0"},
            }})
        elif method in ("notifications/initialized", "notifications/cancelled"):
            pass  # no response for notifications
        elif method == "tools/list":
            _send({"jsonrpc":"2.0","id":id_,"result":{"tools": TOOL_DEFS}})
        elif method == "tools/call":
            name    = params.get("name", "")
            handler = HANDLERS.get(name)
            if not handler:
                _send({"jsonrpc":"2.0","id":id_,"error":{"code":-32601,"message":f"Unknown tool: {name}"}})
                continue
            try:
                result = handler(params.get("arguments", {}))
                _send({"jsonrpc":"2.0","id":id_,"result":{
                    "content": [{"type":"text","text":json.dumps(result, indent=2)}],
                    "isError": "error" in result,
                }})
            except Exception as e:
                _send({"jsonrpc":"2.0","id":id_,"error":{"code":-32603,"message":str(e)}})
        elif id_ is not None:
            _send({"jsonrpc":"2.0","id":id_,"error":{"code":-32601,"message":f"Unknown method: {method}"}})


def main():
    parser = argparse.ArgumentParser(prog="miniloop-mcp")
    parser.add_argument("--port", type=int, default=None,
                        help="Serve SSE on this port instead of stdio")
    a = parser.parse_args()
    if a.port:
        _serve_sse(a.port)
    else:
        serve_stdio()


def _serve_sse(port: int) -> None:
    """Minimal SSE transport for browser/curl testing."""
    try:
        from flask import Flask, Response, request as freq
    except ImportError:
        print("SSE mode needs flask: pip install flask", file=sys.stderr)
        sys.exit(1)

    app = Flask(__name__)
    init_db()

    @app.route("/sse")
    def sse():
        def stream():
            yield "data: {\"type\":\"endpoint\",\"endpoint\":\"/message\"}\n\n"
        return Response(stream(), mimetype="text/event-stream")

    @app.route("/message", methods=["POST"])
    def message():
        req = freq.get_json(force=True)
        # reuse stdio logic by routing through same handlers
        method = req.get("method", "")
        params = req.get("params", {})
        if method == "tools/call":
            name    = params.get("name", "")
            handler = HANDLERS.get(name)
            if handler:
                result = handler(params.get("arguments", {}))
                return {"content": [{"type":"text","text":json.dumps(result, indent=2)}]}
        return {"error": "unsupported"}

    print(f"miniloop MCP SSE server on http://localhost:{port}/sse", file=sys.stderr)
    app.run(port=port)


if __name__ == "__main__":
    main()
