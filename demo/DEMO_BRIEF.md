# miniloop Demo Brief

## What this demo is for

A short video showing miniloop in action inside IBM Bob — no external API key,
no terminal commands, just Bob chat calling MCP tools to autonomously find and
fix a bug in a frontend project.

## The story arc (3 scenes, ~5 min)

1. **Show the broken app** — open it in a browser, the bug is immediately obvious
2. **Hand it to the agent** — type one sentence in Bob chat, watch it loop
3. **Show the audit trail** — `loop_replay` returns every action the agent took

---

## What the "broken project" needs to be

### Format
A self-contained web app — plain HTML/CSS/JS or a simple Python Flask app that
serves a page. No build step, no npm install. It must open in a browser in one
command so the viewer sees the bug immediately on camera.

### The bug — requirements
- **Visually obvious** — wrong number, wrong color, broken layout, or missing
  element that a viewer can see at a glance without reading code
- **Single-file or two-file** — the fix should be a one-line or two-line change
  so the agent's edit is crisp and clear on screen
- **Good examples:**
  - Shopping cart where total is wrong (multiplication bug: `price * quantity - discount` instead of `+ discount`)
  - Dashboard where a percentage shows `1200%` instead of `12%` (off-by-100)
  - Button that does nothing because an event listener is wired to the wrong ID
  - Color-coded status badge that shows red for "success" and green for "error"
    (logic is inverted)
  - A counter app that decrements instead of increments

### The test suite — requirements
- **Must use pytest** — miniloop's `bash` tool runs `python3 -m pytest -v`
- Tests must **fail** before the fix and **pass** after — this is the criteria gate moment
- Keep it to 3–5 tests, each testing one observable behaviour
- Tests should use `requests` or `subprocess` to hit the running app, OR
  extract and test the pure logic function directly (easier, recommended)
- Example: pull the `calculate_total()` function out of the JS/Python into a
  `logic.py` file, test that directly — avoids browser automation complexity

### Recommended structure
```
demo/
├── app.py          Flask app (or just serve static files)
├── logic.py        Pure business logic (also imported by app.py and tests)
├── templates/
│   └── index.html  The UI with the visual bug
├── static/
│   └── style.css
└── tests/
    └── test_logic.py   pytest suite — fails before fix, passes after
```

---

## The demo script (what to say/show)

### Scene 1 — The broken app (~30 sec)
```bash
cd demo && python3 app.py
# open http://localhost:5000 in browser
```
Point at the screen: "You can see right here — the total is wrong / the button
does nothing / the percentage is way off. This is a real bug."

### Scene 2 — Hand it to the agent (~3 min, the money shot)
In Bob chat, type:
```
Use the miniloop MCP tools to find and fix the bug.
goal: "The UI is showing incorrect values — find the bug in logic.py and fix it so all tests pass"
workspace: "/path/to/demo"
```

Watch Bob:
1. `loop_start` → session created, planner generates tasks + criteria
2. `loop_tool("list_files")` → sees the project structure
3. `loop_tool("bash", "pytest -v")` → tests fail, sees exactly which assertion
4. `loop_tool("read_file", "logic.py")` → reads the bug
5. `loop_tool("edit_file")` → fixes the one line
6. `loop_tool("bash", "pytest -v")` → all green ✅
7. `loop_finish` → criteria gate runs pytest independently → `passed: true`

**The key moment to linger on:** `loop_finish` being REJECTED if tests still
fail, then ACCEPTED once they pass. That's the differentiator.

### Scene 3 — The audit trail (~30 sec)
In Bob chat:
```
Show me the event log for that session using loop_replay
```
"Every single action the agent took — permanently recorded. Tool, arguments,
result, timestamp. This is what makes it enterprise-ready."

---

## After making the broken project

1. Test the failure manually:
   ```bash
   cd demo && python3 -m pytest tests/ -v
   # should show FAILED
   ```

2. Introduce the bug path into `demo/WORKSPACE_PATH.txt`:
   ```
   /Users/bradencruz/Projects/agent-loops/demo
   ```
   (so the demo script has the exact path to paste into Bob chat)

3. Make sure `python3 app.py` starts a server with zero extra dependencies
   beyond `flask` (already in requirements.txt)

---

## What miniloop needs from the project

The criteria gate runs a shell command to verify the fix. The project should
have at least one criterion with `check_cmd: "python3 -m pytest tests/ -q"`.
The planner will generate this automatically if the goal mentions "tests pass" —
or you can hardcode it by calling:

```
loop_tool(session_id, "update_ledger", {
  "criteria": [{
    "id": "SC1",
    "description": "All pytest tests pass",
    "check_cmd": "python3 -m pytest tests/ -q"
  }]
})
```

---

## TL;DR for the agent building the project

Make a Flask app with an obvious visual bug. Extract the buggy logic into
`logic.py`. Write `tests/test_logic.py` with pytest that fails before the fix.
The bug should be one line. The fix should be one line. Keep it visual.
