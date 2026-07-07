"""
Loop engine — Phase 2: core model↔tool cycle.
Phase 3 adds: planner, ledger injection, completion gate.
Phase 4 adds: approvals, limits, stall guard.
Phase 5 adds: checkpoints, resume.
Phase 6 adds: critic, TUI polish.
"""
import json
import time
import uuid
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

import miniloop.db as db
from miniloop import config
from miniloop.llm import complete
from miniloop.tools import SCHEMAS, EFFECT, dispatch

console = Console()

SYSTEM_PROMPT = """You are miniloop, an autonomous software engineer working in a scratch workspace.

IMPORTANT: You MUST use tools. Do not answer in prose. Do not ask questions. Just act.

Workflow — follow this exactly:
1. Call list_files to see the workspace
2. Call read_file on any relevant source and test files
3. Call bash with "python3 -m pytest -v" (NOT make, NOT npm) to run tests and see failures
4. Call read_file on files you need to fix
5. Call edit_file to apply the fix
6. Call bash with "python3 -m pytest -v" again to verify the fix works
7. Only call finish() AFTER bash shows pytest passing with 0 failures

Rules:
- Use "python3 -m pytest -v" to run tests — never assume tests pass without running them
- ALWAYS call list_files first — never guess filenames
- If a file doesn't exist, it will show in list_files — use the EXACT filename shown
- If edit_file says old_str not found, call read_file first to get exact content
- finish() will be REJECTED if tests are not actually passing — always run pytest before calling it"""


def _ledger_reminder(session_id: str) -> str:
    """Build a brief ledger status block injected every turn."""
    tasks    = db.get_tasks(session_id)
    criteria = db.get_criteria(session_id)
    if not tasks and not criteria:
        return ""
    lines = ["--- CURRENT LEDGER ---"]
    for t in tasks:
        lines.append(f"[TASK {t['id']}] {t['status'].upper()} — {t['title']}")
    for c in criteria:
        lines.append(f"[CRIT {c['id']}] {c['status'].upper()} — {c['description']}")
    lines.append("--- END LEDGER ---")
    return "\n".join(lines)


def _run_criteria(session_id: str, workspace: str) -> tuple[bool, list]:
    """Run all criteria check_cmds. Returns (all_passed, list of (crit, passed, output))."""
    criteria = db.get_criteria(session_id)
    if not criteria:
        return True, []
    results = []
    all_passed = True
    for c in criteria:
        import subprocess
        try:
            proc = subprocess.run(
                c["check_cmd"], shell=True, cwd=workspace,
                capture_output=True, text=True, timeout=60,
            )
            passed = proc.returncode == 0
            output = (proc.stdout + proc.stderr)[:2000]
        except Exception as e:
            passed = False
            output = str(e)
        status = "passed" if passed else "failed"
        db.upsert_criterion(session_id, c["id"], c["description"],
                            c["check_cmd"], status, output)
        results.append((c, passed, output))
        if not passed:
            all_passed = False
    return all_passed, results


def _handle_update_ledger(session_id: str, args: dict, plan_version: int = 1) -> dict:
    """Process update_ledger tool call — writes to DB directly."""
    tasks    = args.get("tasks")    or []
    criteria = args.get("criteria") or []
    for t in tasks:
        db.upsert_task(
            session_id=session_id,
            task_id=t["id"],
            title=t["title"],
            status=t.get("status", "pending"),
            depends_on=t.get("depends_on", []),
            plan_version=plan_version,
        )
    for c in criteria:
        db.upsert_criterion(
            session_id=session_id,
            crit_id=c["id"],
            description=c["description"],
            check_cmd=c["check_cmd"],
            status=c.get("status", "pending"),
            last_output="",
        )
    summary = f"updated {len(tasks)} task(s), {len(criteria)} criterion(a)"
    return {"ok": True, "output": summary, "error": None}


def run_session(goal: Optional[str], opts: dict) -> None:
    session_id = opts["session_id"]
    workspace  = opts["workspace"]
    autonomy   = opts.get("autonomy", "supervised")

    # ── resume: load existing messages from latest checkpoint ────────────────
    if opts.get("resume"):
        _resume_session(session_id, opts)
        return

    # ── new session ──────────────────────────────────────────────────────────
    db.update_session(session_id, status="running")
    db.insert_event(session_id, 0, "system", {"event": "session_start", "goal": goal})

    messages = [{"role": "user", "content": goal}]

    _loop(session_id, goal, workspace, autonomy, opts, messages, start_turn=1)


def _resume_session(session_id: str, opts: dict) -> None:
    from miniloop.db import get_session, get_latest_checkpoint
    row = get_session(session_id)
    if not row:
        console.print(f"[red]Session {session_id} not found.[/red]")
        return
    if row["status"] == "success":
        console.print(f"[yellow]Session {session_id} already completed successfully.[/yellow]")
        return

    cp = get_latest_checkpoint(session_id)
    if not cp:
        console.print(f"[red]No checkpoint found for {session_id}.[/red]")
        return

    messages = json.loads(cp["messages_json"])
    resume_turn = cp["turn"] + 1

    # Apply any limit overrides
    db.update_session(session_id,
                      status="running",
                      max_turns=opts["max_turns"],
                      max_budget_usd=opts["max_budget"])

    # Force re-orientation
    messages.append({
        "role": "user",
        "content": (
            "Session resumed. Re-verify the current state of the workspace with tools "
            "before continuing — do not trust your memory of file contents."
        ),
    })
    db.insert_event(session_id, resume_turn, "system", {"event": "resumed"})
    console.print(Panel(f"[cyan]Resuming session [bold]{session_id[:8]}…[/bold][/cyan]"))

    _loop(session_id, opts["goal"], opts["workspace"], opts["autonomy"],
          opts, messages, start_turn=resume_turn)


def _loop(session_id: str, goal: str, workspace: str, autonomy: str,
          opts: dict, messages: list, start_turn: int = 1) -> None:

    # Session-wide bash allowlist (for 'a' approval)
    bash_allowlist: set[str] = set()
    # Stall detection: last N tool call fingerprints
    recent_calls: list[str] = []
    stall_injected = False

    turn = start_turn - 1  # incremented at top of loop

    try:
        while True:
            turn += 1

            # ── re-read session for current limits ───────────────────────────
            session_row = db.get_session(session_id)
            max_turns  = session_row["max_turns"]
            max_budget = session_row["max_budget_usd"]
            cost_so_far = session_row["cost_usd"]
            turn_count  = session_row["turn_count"]

            # ── limit checks ─────────────────────────────────────────────────
            if turn_count >= max_turns:
                _suspend(session_id, "max_turns", opts)
                return
            if cost_so_far >= max_budget:
                _suspend(session_id, "max_budget", opts)
                return

            # ── inject ledger reminder ────────────────────────────────────────
            reminder = _ledger_reminder(session_id)
            system = SYSTEM_PROMPT
            if reminder:
                system = SYSTEM_PROMPT + "\n\n" + reminder

            # ── model call ───────────────────────────────────────────────────
            text_blocks, tool_blocks, usage = complete(messages, SCHEMAS, system)

            # Track cost
            input_cost  = usage.input_tokens  * config.INPUT_COST_PER_TOKEN
            output_cost = usage.output_tokens * config.OUTPUT_COST_PER_TOKEN
            turn_cost   = input_cost + output_cost
            new_cost    = cost_so_far + turn_cost
            new_turns   = turn_count + 1
            db.update_session(session_id, cost_usd=new_cost, turn_count=new_turns)

            # Print model text
            for tb in text_blocks:
                if tb.text.strip():
                    console.print(Text(tb.text, style="default"), end="")
            if text_blocks:
                console.print()

            # Log assistant event
            db.insert_event(session_id, turn, "assistant", {
                "text": " ".join(tb.text for tb in text_blocks),
                "tool_calls": [{"name": b.name, "id": b.id} for b in tool_blocks],
                "cost_usd": turn_cost,
            })

            # ── no tool calls → candidate completion ─────────────────────────
            if not tool_blocks:
                _handle_completion(session_id, workspace, messages, turn, goal, opts)
                return

            # ── execute tool calls ────────────────────────────────────────────
            tool_results_content = []
            finish_requested = False
            finish_summary   = ""

            for block in tool_blocks:
                tool_name = block.name
                args      = block.input
                call_id   = str(uuid.uuid4())

                # Stall detection
                fingerprint = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
                recent_calls.append(fingerprint)
                if len(recent_calls) > 4:
                    recent_calls.pop(0)
                if len(recent_calls) == 4 and len(set(recent_calls)) == 1:
                    if not stall_injected:
                        stall_injected = True
                        console.print(Panel(
                            "[yellow]⚠ Stall detected — same tool+args repeated 4×. "
                            "Injecting redirect.[/yellow]"
                        ))
                        messages.append({
                            "role": "user",
                            "content": (
                                "You appear to be repeating yourself. "
                                "Step back, reread the ledger, and try a different approach."
                            ),
                        })
                    else:
                        _suspend(session_id, "stalled", opts)
                        return

                # ── finish intercept ─────────────────────────────────────────
                if tool_name == "finish":
                    finish_requested = True
                    finish_summary   = args.get("summary", "")
                    tool_result = {"ok": True, "output": finish_summary, "error": None}
                    _log_tool_call(call_id, session_id, turn, tool_name, args,
                                   EFFECT[tool_name], "allowed", tool_result, 0)
                    tool_results_content.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(tool_result),
                    })
                    continue

                # ── update_ledger intercept ───────────────────────────────────
                if tool_name == "update_ledger":
                    t0 = time.monotonic()
                    tool_result = _handle_update_ledger(session_id, args)
                    dur = int((time.monotonic() - t0) * 1000)
                    _log_tool_call(call_id, session_id, turn, tool_name, args,
                                   EFFECT[tool_name], "allowed", tool_result, dur)
                    console.print(f"  [dim]⚙ update_ledger → {tool_result['output']}[/dim]")
                    tool_results_content.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(tool_result),
                    })
                    continue

                # ── approval gate ─────────────────────────────────────────────
                effect  = EFFECT.get(tool_name, "read")
                verdict = _get_approval(tool_name, args, effect, autonomy, bash_allowlist)

                if verdict == "denied":
                    tool_result = {"ok": False, "output": "",
                                   "error": "Denied by user."}
                    _log_tool_call(call_id, session_id, turn, tool_name, args,
                                   effect, "denied", tool_result, 0)
                    db.insert_approval(call_id, "denied", "cli-user")
                    console.print(f"  [red]✗ {tool_name} denied[/red]")
                    tool_results_content.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(tool_result),
                    })
                    continue

                if verdict in ("approved", "always"):
                    db.insert_approval(call_id, "approved", "cli-user")
                    if verdict == "always" and tool_name == "bash":
                        first_token = args.get("command", "").split()[0]
                        if first_token:
                            bash_allowlist.add(first_token)

                # ── execute ───────────────────────────────────────────────────
                t0 = time.monotonic()
                tool_result = dispatch(tool_name, args, workspace, session_id)
                dur = int((time.monotonic() - t0) * 1000)

                _log_tool_call(call_id, session_id, turn, tool_name, args,
                               effect, verdict, tool_result, dur)

                # Print compact tool line
                status_icon = "✔" if tool_result["ok"] else "✗"
                color = "green" if tool_result["ok"] else "red"
                arg_preview = _arg_preview(tool_name, args)
                console.print(
                    f"  [{color}]{status_icon}[/{color}] [bold]{tool_name}[/bold]"
                    f"[dim]: {arg_preview}[/dim] [dim]{dur}ms[/dim]"
                )
                if not tool_result["ok"]:
                    # Show error output on failure
                    err_text = (tool_result.get("error") or "") + "\n" + tool_result.get("output","")
                    console.print(f"    [red]{err_text.strip()[:500]}[/red]")

                tool_results_content.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(tool_result),
                })

            # ── append assistant + tool results to messages (OpenAI format) ──
            # Assistant message: text content + tool_calls array
            assistant_msg: dict = {"role": "assistant"}
            combined_text = " ".join(tb.text for tb in text_blocks).strip()
            if combined_text:
                assistant_msg["content"] = combined_text
            if tool_blocks:
                assistant_msg["tool_calls"] = [
                    {
                        "id":       b.id,
                        "type":     "function",
                        "function": {
                            "name":      b.name,
                            "arguments": json.dumps(b.input),
                        },
                    }
                    for b in tool_blocks
                ]
            messages.append(assistant_msg)

            # Tool result messages: one per tool call (role: "tool")
            for tr in tool_results_content:
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tr["tool_use_id"],
                    "content":      tr["content"],
                })

            # ── checkpoint every turn (Phase 5 hook — DB write) ───────────────
            db.insert_checkpoint(
                session_id, turn,
                db.max_event_seq(session_id),
                messages,
            )

            # ── status line ───────────────────────────────────────────────────
            console.print(
                f"[dim]  turn {new_turns}/{max_turns} · "
                f"${new_cost:.4f}/${max_budget:.2f}[/dim]"
            )

            # ── finish gate ───────────────────────────────────────────────────
            if finish_requested:
                _handle_completion(session_id, workspace, messages, turn, goal, opts,
                                   finish_summary=finish_summary)
                return

    except KeyboardInterrupt:
        db.insert_checkpoint(session_id, turn, db.max_event_seq(session_id), messages)
        db.update_session(session_id, status="suspended")
        db.insert_event(session_id, turn, "system",
                        {"event": "suspended", "reason": "user_interrupt"})
        console.print(Panel(
            f"[yellow]⏸ Session suspended (Ctrl-C)\n"
            f"Resume: python -m miniloop --resume {session_id}[/yellow]",
            title="Suspended"
        ))


def _handle_completion(session_id: str, workspace: str, messages: list,
                       turn: int, goal: str, opts: dict,
                       finish_summary: str = "") -> None:
    """Run criteria gate; succeed or re-inject failure."""
    all_passed, results = _run_criteria(session_id, workspace)

    if not results:
        # No criteria defined — accept finish()
        _mark_success(session_id, finish_summary, opts)
        return

    if all_passed:
        # Print green criteria table
        for c, passed, output in results:
            console.print(f"  [green]✅ {c['id']}: {c['description']}[/green]")
        _mark_success(session_id, finish_summary, opts)
        return

    # Criteria failed — reject finish(), inject failure message and continue
    db.insert_event(session_id, turn, "eval", {
        "event": "finish_rejected",
        "results": [(c["id"], passed, output[:200]) for c, passed, output in results],
    })

    failing = []
    for c, passed, output in results:
        if not passed:
            console.print(f"  [red]✗ {c['id']}: {c['description']}[/red]")
            failing.append(
                f"{c['id']}: {c['description']}\n"
                f"  cmd: {c['check_cmd']}\n"
                f"  output (last 30 lines):\n"
                + "\n".join(output.splitlines()[-30:])
            )

    rejection = (
        "finish() rejected. Failing criteria:\n\n"
        + "\n\n".join(failing)
        + "\n\nFix them and call finish() again."
    )
    messages.append({"role": "user", "content": rejection})

    console.print(Panel(
        f"[red]finish() rejected — {len(failing)} criterion(a) still failing.[/red]",
        title="Criteria gate"
    ))

    # Continue the loop
    _loop(session_id, goal, workspace, opts.get("autonomy", "supervised"),
          opts, messages, start_turn=turn + 1)


def _mark_success(session_id: str, summary: str, opts: dict) -> None:
    db.update_session(session_id, status="success")
    db.insert_event(session_id, 0, "result", {"status": "success", "summary": summary})
    row = db.get_session(session_id)
    console.print(Panel(
        f"[green bold]✅ Goal completed[/green bold]\n\n"
        f"{summary}\n\n"
        f"turns: {row['turn_count']}  ·  cost: ${row['cost_usd']:.4f}\n"
        f"workspace: {row['workspace_path']}",
        title="[green]SUCCESS[/green]",
    ))


def _suspend(session_id: str, reason: str, opts: dict) -> None:
    db.update_session(session_id, status="suspended")
    db.insert_event(session_id, 0, "system",
                    {"event": "suspended", "reason": reason})
    row = db.get_session(session_id)
    console.print(Panel(
        f"[yellow]⏸ Session suspended — {reason}\n\n"
        f"turns: {row['turn_count']}/{row['max_turns']}  ·  "
        f"cost: ${row['cost_usd']:.4f}/${row['max_budget_usd']:.2f}\n\n"
        f"Resume: python -m miniloop --resume {session_id} --max-turns +20[/yellow]",
        title="[yellow]SUSPENDED[/yellow]",
    ))


def _get_approval(tool_name: str, args: dict, effect: str,
                  autonomy: str, allowlist: set) -> str:
    """Returns 'allowed' | 'approved' | 'always' | 'denied'."""
    if autonomy == "auto":
        return "allowed"
    if effect == "read":
        return "allowed"
    if effect == "write":
        # writes inside workspace are auto-allowed
        return "allowed"
    if effect == "execute":
        # Check allowlist
        first_token = args.get("command", "").split()[0] if tool_name == "bash" else ""
        if first_token in allowlist:
            return "allowed"
        # Prompt
        cmd_preview = args.get("command", "")[:80]
        console.print(
            f"\n[yellow bold][APPROVAL][/yellow bold] bash: [italic]{cmd_preview}[/italic]\n"
            f"  [dim]y = allow once  a = always allow  n = deny[/dim]"
        )
        try:
            choice = input("  > ").strip().lower()
        except EOFError:
            choice = "y"
        if choice == "y":
            return "approved"
        elif choice == "a":
            return "always"
        else:
            return "denied"
    return "allowed"


def _log_tool_call(call_id: str, session_id: str, turn: int, tool_name: str,
                   args: dict, effect: str, verdict: str,
                   result: dict, duration_ms: int) -> None:
    db.insert_tool_call(call_id, session_id, turn, tool_name, args,
                        effect, verdict, result, duration_ms)
    db.insert_event(session_id, turn, "tool", {
        "tool": tool_name,
        "args": args,
        "verdict": verdict,
        "ok": result.get("ok"),
        "duration_ms": duration_ms,
    })


def _arg_preview(tool_name: str, args: dict) -> str:
    if tool_name == "bash":
        return args.get("command", "")[:60]
    if tool_name in ("read_file", "write_file", "edit_file"):
        return args.get("path", "")[:60]
    if tool_name == "list_files":
        return args.get("pattern", ".")[:40]
    return str(args)[:60]
