"""
Loop engine — Phases 2-6 complete.
"""
import json
import re
import subprocess
import time
import uuid
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import miniloop.db as db
from miniloop import config
from miniloop.llm import complete
from miniloop.tools import SCHEMAS, EFFECT, dispatch

console = Console()

# ── system prompt ─────────────────────────────────────────────────────────────

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
- Use "python3 -m pytest -v" to run Python tests — never assume tests pass without running them
- ALWAYS call list_files first — never guess filenames
- Use the EXACT filenames shown by list_files — never invent paths
- If edit_file says old_str not found, call read_file first to get exact content
- finish() will be REJECTED if tests are not passing — always verify before calling it"""


# ── planner (Phase 3) ─────────────────────────────────────────────────────────

PLAN_PROMPT = """\
You are a planning assistant. Given a goal, produce a JSON execution plan.
Return ONLY valid JSON — no prose, no markdown fences.

Format:
{
  "tasks": [
    {"id": "T1", "title": "<short action title>", "status": "pending", "depends_on": []}
  ],
  "criteria": [
    {"id": "SC1", "description": "<what must be true>", "check_cmd": "<shell command; exit 0 = pass>"}
  ]
}

Rules:
- 3-7 tasks
- At least 1 criterion with a real runnable shell command (e.g. "python3 -m pytest -q")
- check_cmd must exit 0 when the criterion is satisfied"""


def _run_planner(goal: str, session_id: str, workspace: str) -> None:
    """Make a separate planning LLM call and upsert tasks/criteria into the DB."""
    console.print("\n[bold blue]⟳ Planning…[/bold blue]")
    try:
        text_blocks, _, _ = complete(
            messages=[{"role": "user", "content": f"Goal: {goal}"}],
            tools=[],
            system=PLAN_PROMPT,
        )
        raw = " ".join(b.text for b in text_blocks).strip()

        # Strip code fences defensively
        raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
        raw = re.sub(r"\n?```$", "", raw.strip())

        try:
            plan = json.loads(raw)
        except json.JSONDecodeError:
            # Retry: extract first {...} block
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                plan = json.loads(m.group(0))
            else:
                raise

        tasks    = plan.get("tasks", [])
        criteria = plan.get("criteria", [])

        for t in tasks:
            db.upsert_task(session_id, t["id"], t["title"],
                           t.get("status", "pending"),
                           t.get("depends_on", []), plan_version=1)
        for c in criteria:
            db.upsert_criterion(session_id, c["id"], c["description"],
                                c["check_cmd"], "pending", "")

        db.insert_event(session_id, 0, "plan",
                        {"tasks": tasks, "criteria": criteria})

        # Render plan table
        table = Table(title="Plan", show_lines=True)
        table.add_column("ID",    style="dim", width=5)
        table.add_column("Task",  min_width=30)
        table.add_column("Status", width=12)
        for t in tasks:
            table.add_row(t["id"], t["title"], t.get("status", "pending"))
        console.print(table)

        ctable = Table(title="Success Criteria", show_lines=True)
        ctable.add_column("ID",    style="dim", width=5)
        ctable.add_column("Description", min_width=30)
        ctable.add_column("Check command")
        for c in criteria:
            ctable.add_row(c["id"], c["description"], c["check_cmd"])
        console.print(ctable)

    except Exception as e:
        console.print(f"[yellow]⚠ Planner failed ({e}) — continuing without plan[/yellow]")


# ── critic (Phase 6) ──────────────────────────────────────────────────────────

CRITIC_PROMPT = """\
You are a critic evaluating an autonomous agent's progress.
Given the goal, current ledger, and last tool results, answer ONLY with JSON:
{"on_track": true|false, "concern": "<one sentence or empty>", "recommendation": "continue"|"redirect"}
No prose, no fences."""


def _run_critic(goal: str, session_id: str, last_results: list) -> Optional[dict]:
    """Fresh-context critic call — no conversation history."""
    ledger = _ledger_reminder(session_id)
    results_summary = "\n".join(
        f"- {r.get('tool','?')}: {'ok' if r.get('ok') else 'FAILED'} {str(r.get('output',''))[:120]}"
        for r in last_results[-3:]
    )
    prompt = f"Goal: {goal}\n\nLedger:\n{ledger}\n\nLast tool results:\n{results_summary}"
    try:
        text_blocks, _, _ = complete(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            system=CRITIC_PROMPT,
        )
        raw = " ".join(b.text for b in text_blocks).strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.strip())
        return json.loads(raw)
    except Exception:
        return None


# ── helpers ───────────────────────────────────────────────────────────────────

def _ledger_reminder(session_id: str) -> str:
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
    """Run all criteria check_cmds. Returns (all_passed, [(crit, passed, output)])."""
    criteria = db.get_criteria(session_id)
    if not criteria:
        return True, []
    results, all_passed = [], True
    for c in criteria:
        try:
            # Inject venv bin so pytest is available
            from miniloop.tools import _VENV_BIN
            env = __import__("os").environ.copy()
            env["PATH"] = _VENV_BIN + ":" + env.get("PATH", "")
            proc = subprocess.run(
                c["check_cmd"], shell=True, cwd=workspace,
                capture_output=True, text=True, timeout=60, env=env,
            )
            passed = proc.returncode == 0
            output = (proc.stdout + proc.stderr)[:2000]
        except Exception as e:
            passed, output = False, str(e)
        status = "passed" if passed else "failed"
        db.upsert_criterion(session_id, c["id"], c["description"],
                            c["check_cmd"], status, output)
        results.append((c, passed, output))
        if not passed:
            all_passed = False
    return all_passed, results


def _handle_update_ledger(session_id: str, args: dict, plan_version: int = 1) -> dict:
    for t in args.get("tasks") or []:
        db.upsert_task(session_id, t["id"], t["title"],
                       t.get("status", "pending"), t.get("depends_on", []),
                       plan_version)
    for c in args.get("criteria") or []:
        db.upsert_criterion(session_id, c["id"], c["description"],
                            c["check_cmd"], c.get("status", "pending"), "")
    tasks = args.get("tasks") or []
    criteria = args.get("criteria") or []
    return {"ok": True, "output": f"updated {len(tasks)} task(s), {len(criteria)} criterion(a)", "error": None}


def _arg_preview(tool_name: str, args: dict) -> str:
    if tool_name == "bash":
        return args.get("command") or args.get("cmd", "")[:60]
    if tool_name in ("read_file", "write_file", "edit_file"):
        return args.get("path", "")[:60]
    if tool_name == "list_files":
        return args.get("pattern", ".")[:40]
    return str(args)[:60]


def _log_tool_call(call_id, session_id, turn, tool_name, args,
                   effect, verdict, result, duration_ms):
    db.insert_tool_call(call_id, session_id, turn, tool_name, args,
                        effect, verdict, result, duration_ms)
    db.insert_event(session_id, turn, "tool", {
        "tool": tool_name, "args": args, "verdict": verdict,
        "ok": result.get("ok"), "duration_ms": duration_ms,
    })


# ── entry points ──────────────────────────────────────────────────────────────

def run_session(goal: Optional[str], opts: dict) -> None:
    session_id = opts["session_id"]
    workspace  = opts["workspace"]
    autonomy   = opts.get("autonomy", "supervised")

    if opts.get("resume"):
        _resume_session(session_id, opts)
        return

    # Header panel
    console.print(Panel(
        f"[bold]{goal}[/bold]\n"
        f"[dim]session {session_id[:8]}  ·  model {config.MODEL}  ·  "
        f"workspace {workspace}[/dim]",
        title="[bold blue]miniloop[/bold blue]",
    ))

    db.update_session(session_id, status="running")
    db.insert_event(session_id, 0, "system", {"event": "session_start", "goal": goal})

    # Phase 3: planning call
    _run_planner(goal, session_id, workspace)

    messages = [{"role": "user", "content": goal}]
    _loop(session_id, goal, workspace, autonomy, opts, messages, start_turn=1)


def _resume_session(session_id: str, opts: dict) -> None:
    row = db.get_session(session_id)
    if not row:
        console.print(f"[red]Session {session_id} not found.[/red]")
        return
    if row["status"] == "success":
        console.print(f"[yellow]Session {session_id} already completed.[/yellow]")
        return
    cp = db.get_latest_checkpoint(session_id)
    if not cp:
        console.print(f"[red]No checkpoint found for {session_id}.[/red]")
        return

    messages    = json.loads(cp["messages_json"])
    resume_turn = cp["turn"] + 1

    db.update_session(session_id, status="running",
                      max_turns=opts["max_turns"],
                      max_budget_usd=opts["max_budget"])
    messages.append({
        "role": "user",
        "content": (
            "Session resumed. Re-verify the current state of the workspace with "
            "tools before continuing — do not trust your memory of file contents."
        ),
    })
    db.insert_event(session_id, resume_turn, "system", {"event": "resumed"})
    console.print(Panel(
        f"[cyan]Resuming [bold]{session_id[:8]}…[/bold]\n"
        f"goal: {opts['goal']}[/cyan]"
    ))
    _loop(session_id, opts["goal"], opts["workspace"], opts["autonomy"],
          opts, messages, start_turn=resume_turn)


# ── main loop ─────────────────────────────────────────────────────────────────

def _loop(session_id: str, goal: str, workspace: str, autonomy: str,
          opts: dict, messages: list, start_turn: int = 1) -> None:

    bash_allowlist:  set   = set()
    recent_calls:    list  = []
    stall_injected:  bool  = False
    last_tool_results: list = []   # fed to critic
    turn = start_turn - 1

    try:
        while True:
            turn += 1

            row        = db.get_session(session_id)
            max_turns  = row["max_turns"]
            max_budget = row["max_budget_usd"]
            cost       = row["cost_usd"]
            turn_count = row["turn_count"]

            # ── limits ───────────────────────────────────────────────────────
            if turn_count >= max_turns:
                _suspend(session_id, "max_turns", opts)
                return
            if cost >= max_budget:
                _suspend(session_id, "max_budget", opts)
                return

            # ── critic every 6 turns ─────────────────────────────────────────
            if turn_count > 0 and turn_count % 6 == 0:
                verdict = _run_critic(goal, session_id, last_tool_results)
                if verdict:
                    icon = "✔" if verdict.get("on_track") else "⚠"
                    console.print(
                        f"  [dim][CRITIC {icon}] {verdict.get('concern','')} "
                        f"→ {verdict.get('recommendation','continue')}[/dim]"
                    )
                    if verdict.get("recommendation") == "redirect" and verdict.get("concern"):
                        messages.append({
                            "role": "user",
                            "content": f"[CRITIC]: {verdict['concern']}",
                        })
                    db.insert_event(session_id, turn, "eval", {"critic": verdict})

            # ── context assembly ─────────────────────────────────────────────
            reminder = _ledger_reminder(session_id)
            system   = SYSTEM_PROMPT + ("\n\n" + reminder if reminder else "")

            # ── model call ───────────────────────────────────────────────────
            text_blocks, tool_blocks, usage = complete(messages, SCHEMAS, system)

            input_cost  = usage.input_tokens  * config.INPUT_COST_PER_TOKEN
            output_cost = usage.output_tokens * config.OUTPUT_COST_PER_TOKEN
            new_cost    = cost + input_cost + output_cost
            new_turns   = turn_count + 1
            db.update_session(session_id, cost_usd=new_cost, turn_count=new_turns)

            for tb in text_blocks:
                if tb.text.strip():
                    console.print(Text(tb.text, style="default"))

            db.insert_event(session_id, turn, "assistant", {
                "text": " ".join(tb.text for tb in text_blocks),
                "tool_calls": [{"name": b.name, "id": b.id} for b in tool_blocks],
                "cost_usd": input_cost + output_cost,
            })

            # ── no tool calls → completion gate ──────────────────────────────
            if not tool_blocks:
                _handle_completion(session_id, workspace, messages, turn, goal, opts)
                return

            # ── execute tools ─────────────────────────────────────────────────
            tool_results_content = []
            finish_requested     = False
            finish_summary       = ""
            last_tool_results    = []

            for block in tool_blocks:
                tool_name = block.name
                args      = block.input
                call_id   = str(uuid.uuid4())

                # stall detection
                fp = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
                recent_calls = (recent_calls + [fp])[-4:]
                if len(recent_calls) == 4 and len(set(recent_calls)) == 1:
                    if not stall_injected:
                        stall_injected = True
                        console.print(Panel("[yellow]⚠ Stall detected — injecting redirect[/yellow]"))
                        messages.append({"role": "user", "content":
                            "You appear to be repeating yourself. "
                            "Step back, reread the ledger, and try a different approach."})
                    else:
                        _suspend(session_id, "stalled", opts)
                        return

                # ── finish intercept ─────────────────────────────────────────
                if tool_name == "finish":
                    finish_requested = True
                    finish_summary   = args.get("summary", "")
                    r = {"ok": True, "output": finish_summary, "error": None}
                    _log_tool_call(call_id, session_id, turn, tool_name, args,
                                   EFFECT[tool_name], "allowed", r, 0)
                    tool_results_content.append({"tool_use_id": block.id, "content": json.dumps(r)})
                    continue

                # ── update_ledger intercept ───────────────────────────────────
                if tool_name == "update_ledger":
                    t0 = time.monotonic()
                    r  = _handle_update_ledger(session_id, args)
                    dur = int((time.monotonic() - t0) * 1000)
                    _log_tool_call(call_id, session_id, turn, tool_name, args,
                                   EFFECT[tool_name], "allowed", r, dur)
                    console.print(f"  [dim]⚙ update_ledger → {r['output']}[/dim]")
                    tool_results_content.append({"tool_use_id": block.id, "content": json.dumps(r)})
                    continue

                # ── approval gate ─────────────────────────────────────────────
                effect  = EFFECT.get(tool_name, "read")
                verdict = _get_approval(tool_name, args, effect, autonomy, bash_allowlist)

                if verdict == "denied":
                    r = {"ok": False, "output": "", "error": "Denied by user."}
                    _log_tool_call(call_id, session_id, turn, tool_name, args,
                                   effect, "denied", r, 0)
                    db.insert_approval(call_id, "denied", "cli-user")
                    console.print(f"  [red]✗ {tool_name} denied[/red]")
                    tool_results_content.append({"tool_use_id": block.id, "content": json.dumps(r)})
                    continue

                if verdict in ("approved", "always"):
                    db.insert_approval(call_id, "approved", "cli-user")
                    if verdict == "always" and tool_name == "bash":
                        first = args.get("command", "").split()[0]
                        if first:
                            bash_allowlist.add(first)

                # ── execute ───────────────────────────────────────────────────
                t0 = time.monotonic()
                r  = dispatch(tool_name, args, workspace, session_id)
                dur = int((time.monotonic() - t0) * 1000)

                _log_tool_call(call_id, session_id, turn, tool_name, args,
                               effect, verdict, r, dur)
                last_tool_results.append({"tool": tool_name, **r})

                icon  = "✔" if r["ok"] else "✗"
                color = "green" if r["ok"] else "red"
                console.print(
                    f"  [{color}]{icon}[/{color}] [bold]{tool_name}[/bold]"
                    f"[dim]: {_arg_preview(tool_name, args)}[/dim] [dim]{dur}ms[/dim]"
                )
                if not r["ok"]:
                    err = (r.get("error") or "") + "\n" + r.get("output", "")
                    console.print(f"    [red]{err.strip()[:500]}[/red]")

                tool_results_content.append({"tool_use_id": block.id, "content": json.dumps(r)})

            # ── build messages (OpenAI format) ────────────────────────────────
            asst: dict = {"role": "assistant"}
            text = " ".join(tb.text for tb in text_blocks).strip()
            if text:
                asst["content"] = text
            if tool_blocks:
                asst["tool_calls"] = [
                    {"id": b.id, "type": "function",
                     "function": {"name": b.name, "arguments": json.dumps(b.input)}}
                    for b in tool_blocks
                ]
            messages.append(asst)
            for tr in tool_results_content:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tr["tool_use_id"],
                    "content": tr["content"],
                })

            # ── checkpoint every turn ─────────────────────────────────────────
            db.insert_checkpoint(session_id, turn,
                                 db.max_event_seq(session_id), messages)

            # ── status line ───────────────────────────────────────────────────
            tasks_done  = sum(1 for t in db.get_tasks(session_id) if t["status"] == "done")
            tasks_total = len(db.get_tasks(session_id))
            console.print(
                f"[dim]  turn {new_turns}/{max_turns} · "
                f"${new_cost:.4f}/${max_budget:.2f} · "
                f"tasks {tasks_done}/{tasks_total}[/dim]"
            )

            if finish_requested:
                _handle_completion(session_id, workspace, messages, turn, goal, opts,
                                   finish_summary=finish_summary)
                return

    except KeyboardInterrupt:
        db.insert_checkpoint(session_id, turn, db.max_event_seq(session_id), messages)
        db.update_session(session_id, status="suspended")
        db.insert_event(session_id, turn, "system", {"event": "suspended", "reason": "user_interrupt"})
        console.print(Panel(
            f"[yellow]⏸ Suspended (Ctrl-C)\n"
            f"Resume: python -m miniloop --resume {session_id}[/yellow]",
            title="Suspended",
        ))


# ── completion gate ───────────────────────────────────────────────────────────

def _handle_completion(session_id, workspace, messages, turn, goal, opts,
                       finish_summary=""):
    all_passed, results = _run_criteria(session_id, workspace)

    if not results:
        _mark_success(session_id, finish_summary, opts)
        return

    if all_passed:
        for c, _, _ in results:
            console.print(f"  [green]✅ {c['id']}: {c['description']}[/green]")
        _mark_success(session_id, finish_summary, opts)
        return

    # Reject finish() — inject failure message and continue
    db.insert_event(session_id, turn, "eval", {
        "event": "finish_rejected",
        "results": [(c["id"], ok, out[:200]) for c, ok, out in results],
    })
    failing = []
    for c, ok, out in results:
        if not ok:
            console.print(f"  [red]✗ {c['id']}: {c['description']}[/red]")
            failing.append(
                f"{c['id']}: {c['description']}\n"
                f"  cmd: {c['check_cmd']}\n"
                f"  output:\n" + "\n".join(out.splitlines()[-30:])
            )
    messages.append({"role": "user", "content":
        "finish() rejected. Failing criteria:\n\n" +
        "\n\n".join(failing) +
        "\n\nFix them and call finish() again."
    })
    console.print(Panel(
        f"[red]finish() rejected — {len(failing)} criterion(a) still failing[/red]",
        title="Criteria gate",
    ))
    _loop(session_id, goal, workspace, opts.get("autonomy", "supervised"),
          opts, messages, start_turn=turn + 1)


def _mark_success(session_id, summary, opts):
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


def _suspend(session_id, reason, opts):
    db.update_session(session_id, status="suspended")
    db.insert_event(session_id, 0, "system", {"event": "suspended", "reason": reason})
    row = db.get_session(session_id)
    console.print(Panel(
        f"[yellow]⏸ Suspended — {reason}\n\n"
        f"turns: {row['turn_count']}/{row['max_turns']}  ·  "
        f"cost: ${row['cost_usd']:.4f}/${row['max_budget_usd']:.2f}\n\n"
        f"Resume: python -m miniloop --resume {session_id} --max-turns +20[/yellow]",
        title="[yellow]SUSPENDED[/yellow]",
    ))


# ── approval gate ─────────────────────────────────────────────────────────────

def _get_approval(tool_name, args, effect, autonomy, allowlist):
    if autonomy == "auto":
        return "allowed"
    if effect in ("read", "write"):
        return "allowed"
    if effect == "execute":
        first = args.get("command", "").split()[0] if tool_name == "bash" else ""
        if first in allowlist:
            return "allowed"
        preview = args.get("command", "")[:80]
        console.print(
            f"\n[yellow bold][APPROVAL][/yellow bold] bash: [italic]{preview}[/italic]\n"
            f"  [dim]y = allow once  a = always allow  n = deny[/dim]"
        )
        try:
            choice = input("  > ").strip().lower()
        except EOFError:
            choice = "y"
        if choice == "y":   return "approved"
        elif choice == "a": return "always"
        else:               return "denied"
    return "allowed"


# ── replay (Phase 6) ──────────────────────────────────────────────────────────

def replay_session(session_id: str) -> None:
    """Pretty-print the event log for a session (audit trail)."""
    events = db.get_events(session_id)
    if not events:
        console.print(f"[red]No events for session {session_id}[/red]")
        return
    table = Table(title=f"Replay: {session_id[:8]}…", show_lines=True)
    table.add_column("seq",   style="dim", width=5)
    table.add_column("turn",  width=4)
    table.add_column("type",  width=12)
    table.add_column("summary")
    for ev in events:
        try:
            p = json.loads(ev["payload"])
            if ev["type"] == "assistant":
                summary = p.get("text", "")[:80] or str([t["name"] for t in p.get("tool_calls", [])])
            elif ev["type"] == "tool":
                summary = f"{p.get('tool','?')} {'✔' if p.get('ok') else '✗'} {p.get('args',{})}"[:100]
            else:
                summary = str(p)[:100]
        except Exception:
            summary = str(ev["payload"])[:100]
        table.add_row(str(ev["seq"]), str(ev["turn"]), ev["type"], summary)
    console.print(table)
