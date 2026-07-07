"""
7 built-in tools — each returns {"ok": bool, "output": str, "error": str|None}.
All file paths are jailed to the session workspace.
"""
import os
import glob as _glob
import subprocess
import textwrap
from pathlib import Path

# ── effect class map ──────────────────────────────────────────────────────────
EFFECT = {
    "read_file":      "read",
    "list_files":     "read",
    "write_file":     "write",
    "edit_file":      "write",
    "bash":           "execute",
    "update_ledger":  "write",
    "finish":         "read",
}

# ── Anthropic tool schemas ─────────────────────────────────────────────────────
SCHEMAS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file in the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to workspace root"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": "List files in the workspace matching a glob pattern (recursive, skips .git).",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern, default '**/*'"},
            },
            "required": [],
        },
    },
    {
        "name": "write_file",
        "description": "Write (create or overwrite) a file in the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "Path relative to workspace root"},
                "content": {"type": "string", "description": "Full file content"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace an exact string in a file. old_str must appear exactly once. "
            "If you are unsure of the exact content, use read_file first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "Path relative to workspace root"},
                "old_str": {"type": "string", "description": "Exact string to replace (must appear exactly once)"},
                "new_str": {"type": "string", "description": "Replacement string"},
            },
            "required": ["path", "old_str", "new_str"],
        },
    },
    {
        "name": "bash",
        "description": (
            "Run a shell command in the workspace directory. "
            "Timeout 60s. Output truncated to 8000 chars (head + tail)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "update_ledger",
        "description": (
            "Update the task ledger and/or success criteria. "
            "Pass only the entries you want to create or update."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "List of task objects: {id, title, status, depends_on}",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id":         {"type": "string"},
                            "title":      {"type": "string"},
                            "status":     {"type": "string",
                                          "enum": ["pending","in_progress","done","blocked","abandoned"]},
                            "depends_on": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["id", "title", "status"],
                    },
                },
                "criteria": {
                    "type": "array",
                    "description": "List of criterion objects: {id, description, check_cmd}",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id":          {"type": "string"},
                            "description": {"type": "string"},
                            "check_cmd":   {"type": "string"},
                            "status":      {"type": "string"},
                        },
                        "required": ["id", "description", "check_cmd"],
                    },
                },
            },
            "required": [],
        },
    },
    {
        "name": "finish",
        "description": (
            "Signal that you believe all success criteria are met and the goal is complete. "
            "The harness will verify every criterion automatically. "
            "Never call this without first running every check_cmd yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Short summary of what was accomplished"},
            },
            "required": ["summary"],
        },
    },
]


# ── helpers ───────────────────────────────────────────────────────────────────

def _jail(workspace: str, path: str) -> str:
    """Resolve path, raise if it escapes workspace."""
    full = os.path.realpath(os.path.join(workspace, path))
    root = os.path.realpath(workspace)
    if not full.startswith(root + os.sep) and full != root:
        raise ValueError(f"Path escapes workspace: {path!r}")
    return full


def _truncate(text: str, limit: int = 8000) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n… [truncated {len(text) - limit} chars] …\n" + text[-half:]


# ── tool implementations ──────────────────────────────────────────────────────

def read_file(workspace: str, path: str) -> dict:
    try:
        full = _jail(workspace, path)
        with open(full, "r", errors="replace") as f:
            content = f.read()
        return {"ok": True, "output": content, "error": None}
    except Exception as e:
        return {"ok": False, "output": "", "error": str(e)}


def list_files(workspace: str, pattern: str = "**/*") -> dict:
    try:
        root = os.path.realpath(workspace)
        matches = _glob.glob(os.path.join(root, pattern), recursive=True)
        files = []
        for m in sorted(matches):
            # skip .git internals
            parts = Path(m).parts
            if ".git" in parts:
                continue
            if os.path.isfile(m):
                files.append(os.path.relpath(m, root))
        return {"ok": True, "output": "\n".join(files) or "(no files)", "error": None}
    except Exception as e:
        return {"ok": False, "output": "", "error": str(e)}


def write_file(workspace: str, path: str, content: str) -> dict:
    try:
        full = _jail(workspace, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return {"ok": True, "output": f"wrote {path}", "error": None}
    except Exception as e:
        return {"ok": False, "output": "", "error": str(e)}


def edit_file(workspace: str, path: str, old_str: str, new_str: str) -> dict:
    try:
        full = _jail(workspace, path)
        with open(full, "r", errors="replace") as f:
            content = f.read()
        count = content.count(old_str)
        if count == 0:
            return {"ok": False, "output": "",
                    "error": "old_str not found in file — use read_file to check exact content"}
        if count > 1:
            return {"ok": False, "output": "",
                    "error": f"old_str appears {count} times; must appear exactly once"}
        new_content = content.replace(old_str, new_str, 1)
        with open(full, "w") as f:
            f.write(new_content)
        return {"ok": True, "output": f"edited {path}", "error": None}
    except Exception as e:
        return {"ok": False, "output": "", "error": str(e)}


# Venv bin injected into PATH so pytest/pip are always found in workspace bash.
# __file__ = miniloop/tools/__init__.py → .parent.parent.parent = project root
_VENV_BIN = str(Path(__file__).parent.parent.parent / ".venv" / "bin")
_PYTHON    = str(Path(__file__).parent.parent.parent / ".venv" / "bin" / "python3")


def bash(workspace: str, command: str) -> dict:
    try:
        # Prepend venv bin and rewrite bare 'python3'/'python' to use the venv
        env = os.environ.copy()
        env["PATH"] = _VENV_BIN + os.pathsep + env.get("PATH", "")
        # rewrite so 'python3 -m pytest' resolves inside the venv
        command = command.replace("python3 ", f"{_PYTHON} ").replace("python ", f"{_PYTHON} ")
        proc = subprocess.run(
            command,
            shell=True,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        combined = proc.stdout + proc.stderr
        output = _truncate(combined)
        return {
            "ok": proc.returncode == 0,
            "output": output,
            "error": None if proc.returncode == 0 else f"exit code {proc.returncode}",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "", "error": "command timed out after 60s"}
    except Exception as e:
        return {"ok": False, "output": "", "error": str(e)}


def update_ledger(workspace: str, session_id: str,
                  tasks: list = None, criteria: list = None) -> dict:
    """Upsert tasks and/or criteria in the DB — called by the loop, not directly."""
    # Actual DB writes are handled in loop.py which has the session_id in scope.
    # This function is a no-op placeholder; loop.py intercepts the tool call directly.
    return {"ok": True, "output": "ledger updated", "error": None}


def finish(workspace: str, summary: str) -> dict:
    """Signals completion intent — the loop intercepts this to run the criteria gate."""
    return {"ok": True, "output": summary, "error": None}


# ── dispatcher ────────────────────────────────────────────────────────────────

def dispatch(tool_name: str, args: dict, workspace: str, session_id: str = None) -> dict:
    """Route a tool call to its implementation."""
    if tool_name == "read_file":
        if "path" not in args:
            return {"ok": False, "output": "", "error": "read_file requires 'path'"}
        return read_file(workspace, args["path"])
    elif tool_name == "list_files":
        return list_files(workspace, args.get("pattern", "**/*"))
    elif tool_name == "write_file":
        if "path" not in args or "content" not in args:
            return {"ok": False, "output": "", "error": "write_file requires 'path' and 'content'"}
        return write_file(workspace, args["path"], args["content"])
    elif tool_name == "edit_file":
        if not all(k in args for k in ("path", "old_str", "new_str")):
            return {"ok": False, "output": "", "error": "edit_file requires 'path', 'old_str', and 'new_str'"}
        return edit_file(workspace, args["path"], args["old_str"], args["new_str"])
    elif tool_name == "bash":
        cmd = args.get("command") or args.get("cmd") or args.get("shell") or ""
        if not cmd:
            return {"ok": False, "output": "", "error": "bash requires a 'command' argument"}
        return bash(workspace, cmd)
    elif tool_name == "update_ledger":
        # Handled in loop.py — should not reach here
        return {"ok": True, "output": "ledger updated", "error": None}
    elif tool_name == "finish":
        return finish(workspace, args.get("summary", ""))
    else:
        return {"ok": False, "output": "", "error": f"unknown tool: {tool_name}"}
