# BOB `/loop` — Hackathon Build Kit
## ERD + Step-by-Step Development Guide (one afternoon, ~4 hours)

**How to use this document:** Each phase below contains a **PROMPT block** you paste directly into IBM BOB, followed by a **CHECKPOINT** — a concrete command you run and the exact result you must see before moving on. Do not skip checkpoints; in a timeboxed build, an unverified phase is where the last hour dies.

---

## 0. Hackathon scope — what you are actually building

You are building **`miniloop`**: a single-process Python CLI that demonstrates the full autonomous loop from the PRD, honestly cut down:

| PRD concept | Afternoon version |
|---|---|
| Loop Engine | One async loop: model → tool calls → results → repeat |
| Task Ledger + success criteria | JSON ledger in SQLite, criteria as shell commands |
| Planner / Executor / Critic | Same model, three prompt modes; critic every 6 turns |
| Tools | `read_file`, `write_file`, `edit_file`, `bash`, `list_files`, `update_ledger`, `finish` |
| Policy engine (OPA) | ❌ → hardcoded rules: y/N approval for `bash` + writes outside workspace |
| Sandbox (gVisor) | ❌ → runs in a scratch workspace dir; `bash` gets a 60s timeout and cwd jail |
| Checkpoints / resume | Event log in SQLite; `--resume <session_id>` replays state |
| Control plane, approvals via Slack, MCP, registry | ❌ cut entirely |
| Stack | Python 3.11, `anthropic` or watsonx SDK, `rich` for TUI, `sqlite3` stdlib |

**Demo you're aiming for (rehearse this):**
```
python -m miniloop "Build a Flask app with /health and /tickets endpoints \
  returning mock Support Insights data, with passing pytest tests"
```
…and the judges watch it plan, code, run tests, fail, fix itself, and print `✅ ALL CRITERIA PASSED` — then you kill it mid-run and `--resume` it. Those two moments (self-correction + resume) are the whole demo.

---

## 1. ERD — the data model

Seven entities, one SQLite file (`miniloop.db`). The event log is the spine: everything else is derivable from it, but we materialize `tasks` and `criteria` for fast reads.

```mermaid
erDiagram
    SESSION ||--o{ EVENT : "emits"
    SESSION ||--o{ TASK : "plans"
    SESSION ||--o{ CRITERION : "must satisfy"
    SESSION ||--o{ TOOL_CALL : "executes"
    SESSION ||--o{ CHECKPOINT : "snapshots"
    EVENT   ||--o| TOOL_CALL : "may describe"
    TOOL_CALL ||--o| APPROVAL : "may require"
    TASK    }o--o{ TASK : "depends_on"

    SESSION {
        text id PK "uuid"
        text goal
        text status "running|success|failed|suspended|cancelled"
        text workspace_path
        int  max_turns
        real max_budget_usd
        real cost_usd
        int  turn_count
        text model
        text created_at
        text updated_at
    }
    EVENT {
        int  seq PK "autoincrement, per-db ordering"
        text session_id FK
        int  turn
        text type "system|plan|assistant|tool|eval|approval|result"
        text payload "JSON"
        text created_at
    }
    TASK {
        text id PK "T1, T2..."
        text session_id FK
        text title
        text status "pending|in_progress|done|blocked|abandoned"
        text depends_on "JSON list of task ids"
        int  plan_version
    }
    CRITERION {
        text id PK "SC1, SC2..."
        text session_id FK
        text description
        text check_cmd "shell command; exit 0 = pass"
        text status "pending|passed|failed|waived"
        text last_output
    }
    TOOL_CALL {
        text id PK "uuid"
        text session_id FK
        int  turn
        text tool_name
        text args "JSON"
        text effect "read|write|execute"
        text verdict "allowed|approved|denied"
        text result "JSON: ok, output, error"
        int  duration_ms
    }
    APPROVAL {
        text tool_call_id PK_FK
        text decision "approved|denied"
        text decided_by "cli-user"
        text decided_at
    }
    CHECKPOINT {
        int  id PK
        text session_id FK
        int  turn
        int  last_event_seq "resume = load events <= this"
        text messages_json "full model message history at this turn"
        text created_at
    }
```

**Design notes BOB should follow:**
- `EVENT.payload` is free JSON — do not over-normalize; the demo needs speed, and the event log doubles as your audit-trail talking point.
- `CHECKPOINT.messages_json` stores the raw model conversation. Resume = load latest checkpoint, rebuild tasks/criteria from DB, continue the loop. Filesystem state persists naturally because the workspace dir survives.
- All timestamps ISO-8601 strings; all IDs text. No ORM — raw `sqlite3` with a thin `db.py`.

---

## 2. Build plan — 6 phases, ~4 hours

```
Phase 1  0:00–0:20  Scaffold + DB + config          CP1: schema exists
Phase 2  0:20–1:10  Core loop + tools                CP2: fixes a bug unaided
Phase 3  1:10–1:40  Planner + ledger + criteria      CP3: plan prints, criteria run
Phase 4  1:40–2:10  Safety: approvals + limits       CP4: denial + budget stop work
Phase 5  2:10–2:40  Checkpoints + resume             CP5: kill -9, resume, completes
Phase 6  2:40–3:20  Critic + TUI polish              CP6: full demo task passes
Buffer   3:20–4:00  Rehearse demo twice, fix papercuts
```

---

### Phase 1 (20 min) — Scaffold, DB, config

**PROMPT FOR BOB:**
```
Create a Python 3.11 project called miniloop with this exact structure:

miniloop/
  __init__.py
  __main__.py      # CLI entry: python -m miniloop "<goal>" [--resume ID]
                   #   [--max-turns 40] [--max-budget 5.0] [--workspace DIR]
  config.py        # reads ANTHROPIC_API_KEY (or WATSONX_* if set), model name
                   # from env MINILOOP_MODEL default "claude-sonnet-4-6"
  db.py            # sqlite3 helpers: init_db(), plus typed insert/query
                   # functions for each table
  models.py        # dataclasses: Session, Event, Task, Criterion, ToolCall,
                   # Checkpoint mirroring the schema below
  llm.py           # one function: complete(messages, tools, system) -> reply
                   #   wrapping the Anthropic Messages API with tool use;
                   #   returns (text_blocks, tool_use_blocks, usage)
  loop.py          # empty stub for now with run_session(goal, opts) signature
  tools/__init__.py

Use uv or plain venv + requirements.txt: anthropic, rich.

db.py must create these tables exactly (SQLite):

sessions(id TEXT PRIMARY KEY, goal TEXT, status TEXT, workspace_path TEXT,
  max_turns INT, max_budget_usd REAL, cost_usd REAL DEFAULT 0,
  turn_count INT DEFAULT 0, model TEXT, created_at TEXT, updated_at TEXT)

events(seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, turn INT,
  type TEXT, payload TEXT, created_at TEXT)

tasks(id TEXT, session_id TEXT, title TEXT, status TEXT,
  depends_on TEXT, plan_version INT, PRIMARY KEY (id, session_id))

criteria(id TEXT, session_id TEXT, description TEXT, check_cmd TEXT,
  status TEXT, last_output TEXT, PRIMARY KEY (id, session_id))

tool_calls(id TEXT PRIMARY KEY, session_id TEXT, turn INT, tool_name TEXT,
  args TEXT, effect TEXT, verdict TEXT, result TEXT, duration_ms INT)

approvals(tool_call_id TEXT PRIMARY KEY, decision TEXT, decided_by TEXT,
  decided_at TEXT)

checkpoints(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, turn INT,
  last_event_seq INT, messages_json TEXT, created_at TEXT)

__main__.py for now: parse args, init_db(), create a session row, create the
workspace dir (default ./workspaces/<session_id>), print the session id,
call loop.run_session stub. No loop logic yet.
```

**✅ CHECKPOINT 1:**
```
python -m miniloop "hello" ; sqlite3 miniloop.db ".tables"
```
Expect: a printed session id, a `workspaces/<id>/` dir, and all 7 tables listed. **Do not proceed without this.**

---

### Phase 2 (50 min) — Core loop + tools  ← the heart, protect this timebox

**PROMPT FOR BOB:**
```
Implement the agentic loop and tools for miniloop.

tools/ — implement as plain functions, each returning
{"ok": bool, "output": str, "error": str|None}, all paths jailed to the
session workspace (resolve and reject anything escaping it):

- read_file(path)                       effect=read
- list_files(glob_pattern=".")          effect=read   (recursive, skip .git)
- write_file(path, content)             effect=write
- edit_file(path, old_str, new_str)     effect=write  (old_str must appear
                                        exactly once, else return error)
- bash(command)                         effect=execute (subprocess, cwd=workspace,
                                        timeout=60s, capture stdout+stderr,
                                        truncate output to 8000 chars keeping
                                        head and tail)
- update_ledger(tasks_json, criteria_json)  effect=write (upsert DB rows)
- finish(summary)                       effect=read   (signals completion intent)

Each tool has an Anthropic tool-use JSON schema in tools/__init__.py, plus
EFFECT = {tool_name: effect} map.

loop.py — run_session(goal, opts):
1. Build system prompt: "You are miniloop, an autonomous engineer. You work
   in a scratch workspace. Loop: inspect state, act via tools, verify with
   real commands. Never claim success without running the check. When every
   success criterion passes, call finish()."
2. messages = [user: goal]. Then loop:
   a. reply = llm.complete(messages, tools, system)
   b. append assistant message; stream text to console
   c. if reply contains tool_use blocks: execute each sequentially,
      log a tool_calls row + event per call, append tool_result blocks
      as the next user message; increment turn; continue
   d. if no tool_use blocks OR finish() was called: break
3. Track cost from usage (input/output token counts * per-token price
   constants in config.py); update sessions.cost_usd and turn_count each turn.
4. Log every step as events rows (type=assistant/tool/result).
5. On loop end: print summary panel (rich): status, turns, cost, workspace.

For now status = "success" whenever finish() is called. Limits and
criteria enforcement come in later phases — leave clear TODO markers.
```

**✅ CHECKPOINT 2 — the self-correction test:**
```
mkdir -p /tmp/buggy && printf 'def add(a,b):\n    return a-b\n' > /tmp/buggy/calc.py
printf 'from calc import add\ndef test_add():\n    assert add(2,3)==5\n' > /tmp/buggy/test_calc.py
python -m miniloop "fix the failing test" --workspace /tmp/buggy
```
Expect to *watch* it: run pytest → see the failure → read calc.py → edit → re-run pytest → green → `finish()`. If it claims success without re-running pytest, tighten the system prompt line "Never claim success without running the check" and re-test. **This checkpoint is your demo's core beat — get it solid.**

---

### Phase 3 (30 min) — Planner, ledger, success criteria

**PROMPT FOR BOB:**
```
Add planning to miniloop.

1. Before the main loop, make a dedicated planning call in loop.py:
   plan_prompt asks the model to return ONLY JSON:
   {"tasks":[{"id":"T1","title":...,"depends_on":[]}...],
    "criteria":[{"id":"SC1","description":...,"check_cmd":"<shell command
    that exits 0 iff satisfied>"}...]}
   Rules in the prompt: 3-7 tasks; every criterion MUST be a runnable shell
   command (e.g. "cd app && python -m pytest -q"); at least one criterion.
   Parse (strip code fences defensively), upsert into tasks/criteria tables,
   log a plan event, and render the plan as a rich table before execution
   starts.

2. Inject the current ledger into every loop iteration: prepend a
   system-side reminder block to the request containing tasks with statuses
   and criteria with statuses, so it survives long conversations.

3. The model updates task status via update_ledger as it works.

4. Completion gate: when the model calls finish(), run every criterion's
   check_cmd in the workspace. All exit 0 -> status "success", print green
   panel per criterion. Any nonzero -> mark failed criteria in DB, DO NOT
   finish; instead append a user message: "finish() rejected. Failing
   criteria:\n<id: cmd + last 30 lines of output>...\nFix them." and
   continue the loop. Log eval events either way.
```

**✅ CHECKPOINT 3:**
```
python -m miniloop "Create fizzbuzz.py printing FizzBuzz 1-30 and a pytest test for it"
```
Expect: plan table renders first; criteria include a real pytest command; at the end each criterion prints ✅; try `sqlite3 miniloop.db "select id,status from criteria"` → all `passed`. Also verify the gate works: the rejection path should be visible at least once across your test runs (if not, temporarily sabotage a criterion to confirm the loop continues instead of finishing).

---

### Phase 4 (30 min) — Approvals + limits (your "enterprise" talking point)

**PROMPT FOR BOB:**
```
Add safety to miniloop.

1. Approval gate in the tool-execution path of loop.py:
   - effect=read: auto-allow.
   - effect=write inside workspace: auto-allow.
   - effect=execute (bash): prompt in the TUI:
       [APPROVAL] bash: <command>   (y = allow once / a = always allow this
       session / n = deny)
     'a' adds the command's first token to a session allowlist (e.g. approving
     "pytest -q" auto-allows future pytest). Denial returns
     {"ok": false, "error": "Denied by user: <reason optional>"} to the model
     and records verdict=denied. Record every decision in approvals table.
   - Add --autonomy auto flag that auto-approves everything (for the demo's
     "look away" moment) but still logs verdicts.

2. Limits, checked at the top of every turn:
   - turn_count >= max_turns -> status "suspended", reason "max_turns"
   - cost_usd >= max_budget_usd -> status "suspended", reason "max_budget"
   On suspend: print an amber panel with the resume command:
   python -m miniloop --resume <session_id> --max-turns +20
   (support "+N" meaning extend, and plain N meaning replace).

3. Loop-stall guard: if the last 4 tool calls are identical (same tool+args
   hash), inject a user message: "You appear to be repeating yourself.
   Step back, reread the ledger, and try a different approach." Once per
   session; second trigger -> suspend with reason "stalled".
```

**✅ CHECKPOINT 4:** Run any goal with default (supervised) mode → you get the y/a/n prompt on the first `bash` and denial visibly makes the model adapt. Then: `python -m miniloop "build anything nontrivial" --max-turns 3` → amber suspension panel with a resume command after turn 3. Check `approvals` table has rows.

---

### Phase 5 (30 min) — Checkpoints + resume (demo moment #2)

**PROMPT FOR BOB:**
```
Add durability to miniloop.

1. After every completed turn in loop.py, write a checkpoints row:
   turn, last_event_seq (max seq for this session), and messages_json =
   json.dumps of the full messages list. Overwrite-style is fine (keep all
   rows; resume uses the latest).

2. Implement --resume <session_id> in __main__.py:
   - load the session row; refuse if status is "success"
   - apply --max-turns/--max-budget overrides ("+N" extends)
   - load latest checkpoint -> messages; reload tasks/criteria from DB
   - set status "running"; append a user message: "Session resumed. Re-verify
     the current state of the workspace with tools before continuing —
     do not trust your memory of file contents." (forces re-orientation)
   - continue the same loop.

3. Also catch KeyboardInterrupt in the loop: checkpoint, mark "suspended"
   with reason "user_interrupt", print the resume command, exit 0. This
   makes Ctrl-C the graceful pause button.
```

**✅ CHECKPOINT 5 — the kill test:** Start the fizzbuzz goal, Ctrl-C mid-loop → suspension panel. Run the printed resume command → it re-lists files, picks up where it left off, finishes with criteria green. Then the brutal version: `kill -9` the process, resume again — still works (checkpoint was written last turn). If the model acts on stale file beliefs after resume, strengthen the re-orientation message.

---

### Phase 6 (40 min) — Critic + TUI polish

**PROMPT FOR BOB:**
```
Final phase for miniloop.

1. Critic: every 6 turns, make a SEPARATE llm call (no conversation
   history) with only: the goal, the ledger (tasks+criteria+statuses), and
   the last 3 tool results summarized. Ask for ONLY JSON:
   {"on_track": bool, "concern": str, "recommendation": "continue"|"redirect"}
   If redirect: inject the concern as a user message prefixed
   "[CRITIC]: ". Log an eval event either way. Render a dim one-line
   critic verdict in the TUI.

2. TUI polish with rich:
   - Header panel: goal, session id, model
   - Live status line: turn N/max · cost $X.XX/$MAX · tasks done/total
   - Tool calls as compact lines:  ⚙ bash: pytest -q … ✔ 1.2s
     (expandable output only on failure)
   - Plan table at start, criteria results table at end, final summary panel

3. Add `python -m miniloop --replay <session_id>`: pretty-print the event
   log in order (turn, type, one-line payload summary). This is the
   "audit trail" beat for judges.

4. Write README.md: 5-line quickstart, the demo commands, and a 6-bullet
   architecture summary (loop, ledger, gate, approvals, checkpoints, critic).
```

**✅ CHECKPOINT 6 — full dress rehearsal:**
```
python -m miniloop "Build a Flask app with /health and /tickets endpoints \
returning mock Support Insights ticket data, with passing pytest tests" \
--autonomy auto --max-budget 3.0
```
Expect end-to-end: plan → scaffold → tests fail at least once → self-fix → all criteria ✅ → summary panel. Then `--replay` shows the full audit trail. **Run it twice** — agent demos have variance; you want to know your failure modes before the judges do.

---

## 3. Demo script (3 minutes)

1. **(20s)** Show the goal command; plan table renders — "it decomposed the goal and wrote *machine-checkable* success criteria."
2. **(60s)** Let it run in `--autonomy auto`; narrate the first test failure and self-correction — "it observed the failure and fixed itself; nobody pasted an error message."
3. **(30s)** Ctrl-C it. Show the suspension panel. Resume it. — "sessions are durable; this is how it survives limits, laptops, and approvals."
4. **(40s)** Show one supervised run with the y/a/n bash approval, then `--replay` — "every action, verdict, and approval is in an audit ledger; this is the enterprise story."
5. **(30s)** Point at the ERD slide: "the event log is the spine — audit, resume, and replay are all the same table."

## 4. If you fall behind — cut in this order

1. Critic (Phase 6.1) — the completion gate already prevents false success.
2. Stall guard (Phase 4.3).
3. `--replay` — the DB itself can be shown with `sqlite3`.
4. **Never cut:** Checkpoint 2 (self-correction) and Phase 3's completion gate. Those two are the difference between "agent" and "chatbot with tools."

## 5. Known papercuts to pre-empt

- **JSON from the planner**: always strip ``` fences and retry once on parse failure with "return only valid JSON, no prose."
- **`edit_file` misses**: models sometimes hallucinate `old_str`; the "must appear exactly once" error message should tell the model to `read_file` first.
- **bash output floods**: the 8000-char head+tail truncation in Phase 2 is not optional — one verbose `npm install` can eat your context and your budget.
- **Cost surprises**: put the per-token prices in `config.py` and glance at the live cost line during rehearsal; set demo budget to 2–3× your rehearsal cost.
