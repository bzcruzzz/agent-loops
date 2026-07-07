"""
CLI entry point: python -m miniloop "<goal>" [options]
"""
import argparse
import os
import sys
import uuid

from miniloop import config
from miniloop.db import init_db, insert_session
from miniloop import loop


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="miniloop")
    parser.add_argument("goal", nargs="?", default=None,
                        help="Goal for the agent to accomplish")
    parser.add_argument("--resume", metavar="SESSION_ID",
                        help="Resume a suspended session by id")
    parser.add_argument("--max-turns", type=str, default=None,
                        help="Max turns (plain N replaces; +N extends)")
    parser.add_argument("--max-budget", type=float, default=None,
                        help="Max spend in USD")
    parser.add_argument("--workspace", metavar="DIR", default=None,
                        help="Workspace directory (default: ./workspaces/<id>)")
    parser.add_argument("--autonomy", choices=["supervised", "auto"],
                        default="supervised",
                        help="supervised = prompt for bash approvals; auto = approve all")
    parser.add_argument("--replay", metavar="SESSION_ID",
                        help="Pretty-print event log for a session and exit")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    # ── replay mode ──────────────────────────────────────────────────────────
    if args.replay:
        from miniloop.db import get_events
        events = get_events(args.replay)
        if not events:
            print(f"No events found for session {args.replay}")
            sys.exit(1)
        from rich.console import Console
        from rich.table import Table
        console = Console()
        table = Table(title=f"Replay: {args.replay[:8]}…", show_lines=True)
        table.add_column("seq", style="dim", width=5)
        table.add_column("turn", width=4)
        table.add_column("type", width=12)
        table.add_column("payload summary")
        import json
        for ev in events:
            try:
                p = json.loads(ev["payload"])
                summary = str(p)[:120]
            except Exception:
                summary = str(ev["payload"])[:120]
            table.add_row(str(ev["seq"]), str(ev["turn"]), ev["type"], summary)
        console.print(table)
        sys.exit(0)

    # ── resume mode ──────────────────────────────────────────────────────────
    if args.resume:
        init_db()
        opts = _build_resume_opts(args)
        loop.run_session(None, opts)
        return

    # ── new session ──────────────────────────────────────────────────────────
    if not args.goal:
        print("error: provide a goal or --resume SESSION_ID", file=sys.stderr)
        sys.exit(1)

    init_db()

    session_id = str(uuid.uuid4())
    workspace = args.workspace or os.path.join(config.WORKSPACES_DIR, session_id)
    os.makedirs(workspace, exist_ok=True)

    max_turns  = config.DEFAULT_MAX_TURNS  if args.max_turns  is None else _parse_turns(args.max_turns, 0)
    max_budget = config.DEFAULT_MAX_BUDGET if args.max_budget is None else args.max_budget

    insert_session(
        session_id=session_id,
        goal=args.goal,
        workspace_path=os.path.abspath(workspace),
        max_turns=max_turns,
        max_budget_usd=max_budget,
        model=config.MODEL,
    )

    print(f"session: {session_id}")

    opts = {
        "session_id":   session_id,
        "goal":         args.goal,
        "workspace":    os.path.abspath(workspace),
        "max_turns":    max_turns,
        "max_budget":   max_budget,
        "autonomy":     args.autonomy,
        "resume":       False,
    }
    loop.run_session(args.goal, opts)


def _parse_turns(value: str, current: int) -> int:
    """Parse --max-turns: '+N' extends current, plain 'N' replaces."""
    value = value.strip()
    if value.startswith("+"):
        return current + int(value[1:])
    return int(value)


def _build_resume_opts(args) -> dict:
    from miniloop.db import get_session
    session_id = args.resume
    row = get_session(session_id)
    if not row:
        print(f"error: session {session_id} not found", file=sys.stderr)
        sys.exit(1)

    current_turns  = row["max_turns"]
    max_turns  = _parse_turns(args.max_turns, current_turns)  if args.max_turns  else current_turns
    max_budget = args.max_budget if args.max_budget else row["max_budget_usd"]

    return {
        "session_id": session_id,
        "goal":       row["goal"],
        "workspace":  row["workspace_path"],
        "max_turns":  max_turns,
        "max_budget": max_budget,
        "autonomy":   args.autonomy,
        "resume":     True,
    }


if __name__ == "__main__":
    main()
