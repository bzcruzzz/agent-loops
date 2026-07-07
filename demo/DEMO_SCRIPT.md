# miniloop Demo Script

## Intro (30 sec)

"AI coding assistants are great at answering questions — but they still make
*you* do the loop. Copy the error, paste it back, explain the context again,
run the fix yourself. You're the orchestrator.

miniloop flips that. It's an autonomous agent loop built for IBM Bob — the
model acts, sees the result, decides what to do next, and keeps going until
the work is actually done. And it runs entirely inside Bob. No API key.
No extra setup. Just Bob."

---

## Scene 1 — The broken app (30 sec)

*[Open browser showing the Flask app with the visual bug]*

"Here's a simple web app with a bug. You can see it immediately —
[point at the wrong value / broken element]. This is what we're handing
to the agent."

*[Open terminal, run pytest]*

"The test suite confirms it — failing."

---

## Scene 2 — Hand it to the agent (3 min)

*[Switch to Bob chat]*

"I'm going to type one sentence."

```
Use the miniloop MCP tools to find and fix the bug.
goal: "The UI is showing incorrect values — find the bug in logic.py and fix it so all tests pass"
workspace: "/path/to/demo"
```

*[Watch Bob work — talk over it]*

"It calls `loop_start` — miniloop creates a session, runs a planner,
generates a task ledger and machine-checkable success criteria.

Now it's looping. `loop_tool` to list files, see the structure.
`loop_tool` to run pytest — sees exactly which assertion fails.
`loop_tool` to read the buggy file — finds the line.
`loop_tool` to edit it — one line fix.
`loop_tool` to run pytest again — green."

*[Pause on loop_finish being called]*

"Now it calls `loop_finish`. But here's the key — miniloop doesn't just
take the model's word for it. It runs the test suite itself, independently,
to verify. If anything's still failing, it rejects the finish and the agent
has to keep going.

This time — passed."

*[Show the browser with the fix applied]*

"And there's the app. Fixed."

---

## Scene 3 — The audit trail (30 sec)

*[Back in Bob chat]*

"One more thing."

```
Show me the event log for that session using loop_replay
```

*[Bob returns the structured event log]*

"Every tool call. Every result. Turn number, timestamp, what changed.
This is the immutable audit trail — not a summary, the actual record.
Every action the agent took, permanently logged."

---

## Outro (20 sec)

"This is miniloop — an autonomous agent loop that runs inside Bob with
no external dependencies. The model reasons, miniloop executes, the
criteria gate enforces correctness, and nothing ships until the tests pass.

That's the loop."
