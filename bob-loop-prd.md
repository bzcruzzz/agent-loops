# IBM BOB `/loop` — Autonomous Agent Platform
## Product Requirements & Engineering Design Document

| Field | Value |
|---|---|
| Document type | PRD / ERD |
| Status | Draft for architecture review |
| Version | 0.9 |
| Audience | BOB platform architects, engineering leads, security & compliance reviewers |
| Codename | **BOB Loop Engine (BLE)** + **BOB Agent Development Kit (BOB ADK)** |

---

## Executive Summary

BOB today is a prompt-response assistant: one request in, one answer out. The market has moved. Cursor Agent Mode, Claude Code, Codex agents, and Replit Agent all run a **closed-loop agentic cycle** — the model acts, observes the result, decides, and repeats until the goal is done. The unit of value has shifted from *answers* to *completed work*.

This document specifies two coupled deliverables:

1. **`/loop`** — a first-class command in BOB (CLI, IDE, and web) that turns any goal into an autonomous, observable, interruptible, resumable execution session governed by enterprise policy.
2. **BOB ADK** — the SDK/framework (Python, TypeScript, Java) that exposes the same loop engine programmatically, so teams can build, test, and deploy their own agents on BOB infrastructure.

The core architectural bet, stated up front so it can be argued with:

> **The loop is model-driven, not workflow-driven.** We do not build a rigid DAG orchestrator with LLM nodes (the LangGraph pattern). We build a single high-quality agentic loop — model proposes tool calls, harness executes them under policy, results feed back — and layer planning, evaluation, and governance *around* that loop as structured artifacts and hooks. This is the pattern that made Claude Code and Cursor win: the model does the reasoning; the harness does safety, state, and observability. Graph-style orchestration is offered as an *optional* ADK layer for teams that need deterministic multi-agent pipelines, not as the foundation.

---

# 1. Product Vision

## 1.1 The problem

**For developers:** Every non-trivial engineering task is a loop — write, run, read the error, fix, repeat. A prompt-response assistant forces the *human* to be the loop: copy the error back in, re-explain context, paste the next file. The human becomes the orchestrator of the machine, which inverts the value proposition. Competitors have removed this friction; BOB has not. Developers who try Cursor Agent Mode or Claude Code for a week do not return to prompt-response tools.

**For enterprises (IBM's actual wedge):** The consumer agent tools are excellent at the loop but weak at the enterprise perimeter:

- No workload-level audit trail that maps agent actions to change tickets.
- Weak or absent policy engines (a YAML allowlist is not governance).
- No approval workflows that integrate with existing change-management (ServiceNow, GitHub Enterprise required reviewers).
- No data-residency, air-gap, or FedRAMP story.
- No first-class connection to enterprise systems of record (Jira, ServiceNow, Salesforce, SAP, mainframe tooling, watsonx governance).

**The gap BOB fills:** *A Claude-Code-quality agent loop wrapped in an enterprise-grade control plane.* That is a product only a company like IBM can credibly ship, and it is the only positioning where BOB doesn't fight Cursor on Cursor's turf.

## 1.2 Why enterprises adopt it

| Enterprise need | `/loop` answer |
|---|---|
| "Who approved this change and why?" | Every session is an immutable, replayable event ledger tied to identity, ticket, and policy version |
| "Agents must not touch prod data" | Policy engine (OPA-based) evaluates every tool call against data classification and environment tags |
| "We can't send code to a public API" | Model gateway supports watsonx.ai on-prem, Anthropic/OpenAI via VPC endpoints, and fully air-gapped deployment |
| "We need SDLC compliance" | Native gates: agent output ships as PRs with required human review; SOC2/ISO evidence auto-generated from the ledger |
| "We have 40 internal tools" | MCP-native tool registry with centrally governed tool catalogs per team/role |
| "We want to build our own agents" | ADK with the same loop, policies, and audit for custom agents — the platform play |

## 1.3 Why developers use it

- **It finishes the job.** `/loop fix the failing CI on this branch` runs tests, reads failures, edits, re-runs, and opens the PR.
- **It's interruptible and steerable.** Live stream of every action; type at any time to redirect mid-loop; pause/resume/fork sessions.
- **It's trustworthy by construction.** Plan shown before execution (configurable), diffs reviewable before commit, one-key rollback of everything a session did.
- **It's programmable.** The same engine behind `/loop` is a three-line SDK call, so a developer's ad-hoc workflow becomes a deployable agent without a rewrite.
- **It knows the IBM estate.** Out-of-the-box tools for GitHub Enterprise, Jenkins/Travis, Artifactory, ServiceNow, Jira, Box, Slack, watsonx — the stuff Cursor will never prioritize.

## 1.4 Non-goals (v1)

- Not a general workflow automation product (that's watsonx Orchestrate's lane; we integrate, not compete).
- Not a no-code agent builder UI in v1 (ADK first; visual builder is future vision, §12).
- Not model training/fine-tuning infrastructure.
- Not autonomous production deployment without human gates (deploy tools exist but always route through approval policy by default).

---

# 2. System Architecture

## 2.1 High-level architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              CLIENTS                                     │
│   BOB CLI (/loop)   BOB IDE plugin   BOB Web   CI runner   ADK apps      │
└───────────────┬──────────────────────────────────────────────────────────┘
                │  gRPC / SSE (session stream protocol)
┌───────────────▼──────────────────────────────────────────────────────────┐
│                          CONTROL PLANE                                   │
│  ┌────────────┐ ┌──────────────┐ ┌───────────────┐ ┌──────────────────┐ │
│  │ Session    │ │ Policy Engine│ │ Approval      │ │ Agent Registry   │ │
│  │ Manager    │ │ (OPA/Rego)   │ │ Service       │ │ (agents, tools,  │ │
│  │ (lifecycle,│ │ per-tool-call│ │ (human gates, │ │  skills, MCP     │ │
│  │  resume,   │ │ evaluation   │ │  ServiceNow/  │ │  catalog)        │ │
│  │  fork)     │ │              │ │  Slack)       │ │                  │ │
│  └────────────┘ └──────────────┘ └───────────────┘ └──────────────────┘ │
│  ┌────────────┐ ┌──────────────┐ ┌───────────────┐ ┌──────────────────┐ │
│  │ Event      │ │ Identity &   │ │ Budget/Quota  │ │ Observability    │ │
│  │ Ledger     │ │ Secrets      │ │ Service       │ │ (OTel collector, │ │
│  │ (immutable │ │ (SSO, Vault, │ │ (tokens, $,   │ │  traces, evals)  │ │
│  │  log)      │ │  scoped creds│ │  turns)       │ │                  │ │
│  └────────────┘ └──────────────┘ └───────────────┘ └──────────────────┘ │
└───────────────┬──────────────────────────────────────────────────────────┘
                │  schedules / attaches
┌───────────────▼──────────────────────────────────────────────────────────┐
│                           DATA PLANE (per session)                       │
│  ┌─────────────────────────── Loop Runner Pod ───────────────────────┐  │
│  │  ┌───────────────┐   ┌──────────────┐   ┌───────────────────────┐ │  │
│  │  │  LOOP ENGINE  │──▶│ Tool Router  │──▶│  Sandbox (gVisor/     │ │  │
│  │  │ plan▸act▸     │◀──│ (local, MCP, │◀──│  Kata VM): fs, shell, │ │  │
│  │  │ observe▸eval  │   │  remote)     │   │  git, build, network  │ │  │
│  │  └──────┬────────┘   └──────────────┘   │  egress allowlist     │ │  │
│  │         │                               └───────────────────────┘ │  │
│  │  ┌──────▼────────┐   ┌──────────────┐   ┌───────────────────────┐ │  │
│  │  │ Context Mgr   │   │ Memory Mgr   │   │ Checkpoint Store      │ │  │
│  │  │ (window, com- │   │ (task ledger,│   │ (fs snapshot + msg    │ │  │
│  │  │  paction)     │   │  notes, KV)  │   │  log per turn)        │ │  │
│  │  └───────────────┘   └──────────────┘   └───────────────────────┘ │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└───────────────┬──────────────────────────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────────────────────────┐
│                          MODEL GATEWAY                                    │
│   watsonx.ai (Granite, hosted 3P)  │  Anthropic  │  OpenAI  │  on-prem   │
│   unified API, prompt caching, failover, per-tenant routing, PII scrub   │
└───────────────────────────────────────────────────────────────────────────┘
```

**Opinionated decisions:**

- **Control plane / data plane split.** Sessions run in isolated, ephemeral sandboxes (data plane). Policy, identity, audit, and approvals live in the multi-tenant control plane. This is what lets one architecture serve laptop-local dev (data plane = local process, control plane = lightweight local daemon) and enterprise SaaS (data plane = Kubernetes pods) with the same code.
- **One loop engine, everywhere.** The CLI, IDE, web, CI, and ADK all drive the identical `loop-core` library. No "lite" loop for the CLI. This is the single most important consistency decision; it's why Claude Code's SDK works.
- **Everything is an event.** The session is a stream of typed events (see §2.4). UI rendering, audit, debugging replay, and evals all consume the same stream. There is no second bookkeeping system to drift.

## 2.2 Runtime architecture

Each session gets a **Loop Runner**: one process (local mode) or one pod (server mode) containing:

| Component | Responsibility |
|---|---|
| Loop Engine | Drives the model↔tool cycle; owns turn semantics, limits, stop conditions |
| Tool Router | Resolves tool calls to local tools, sandbox exec, or MCP servers; enforces policy verdicts |
| Sandbox | gVisor (default) or Kata Containers (high-isolation tier) workspace with the repo checked out, language toolchains, and a network egress allowlist |
| Context Manager | Assembles each model request; compaction; prompt-cache-aware ordering |
| Memory Manager | Task ledger, scratchpad notes, session KV, cross-session memory reads |
| Checkpoint Store | Per-turn snapshot: message log offset + filesystem diff (OverlayFS layer) → enables rewind, fork, resume |

**Execution modes:**

1. **Local** — runner is a process on the dev machine; sandbox is a local container (or bare workspace in `--trust-workspace` mode); control plane is an embedded daemon syncing the ledger when online. Zero-latency dev experience.
2. **Attached remote** — runner in BOB cloud/on-prem cluster; client streams events. Survives laptop sleep; this is how "kick off `/loop` and check back in an hour" works.
3. **Headless** — triggered by API/CI/webhook (e.g., "on Jira ticket labeled `bob-fix`, run agent X"). No client attached; results land as PRs/comments.

## 2.3 Agent lifecycle

```
 DEFINED ──▶ VALIDATED ──▶ REGISTERED ──▶ INSTANTIATED ──▶ RUNNING
 (manifest)  (schema,      (registry,      (session        │
              policy lint)  versioned)      created)        ▼
                                          ┌───────────────────────────┐
                                          │  RUNNING sub-states:      │
                                          │  planning ⇄ executing ⇄   │
                                          │  evaluating ⇄ replanning  │
                                          │  │ waiting_approval       │
                                          │  │ waiting_input          │
                                          │  │ paused                 │
                                          └──────────┬────────────────┘
                                                     ▼
                     COMPLETED │ FAILED │ CANCELLED │ SUSPENDED (resumable)
                                                     │
                                              archived ledger + artifacts
```

Every transition emits a ledger event. `SUSPENDED` is first-class: a session that hits a budget cap or an unanswered approval is suspended with full checkpoint, not killed.

## 2.4 Request flow — `/loop Build a React application for Support Insights`

```
 1. CLI ──goal──▶ Session Manager
 2. Session Manager: authN (SSO) → resolve agent profile (default: "bob-swe")
    → policy snapshot pinned to session → allocate Loop Runner → session_id
 3. Runner: init event {session_id, agent, model, policy_version, tools}
 4. LOOP ENGINE begins (see §4):
      Turn 1  model → GoalSpec + clarifying Qs if ambiguous (AskUser tool)
      Turn 2  model → Plan artifact (task ledger v1) → streamed to client
      Turn N  model → tool calls → Policy Engine verdict per call:
                allow          → Tool Router executes in sandbox
                needs_approval → Approval Service (inline prompt / Slack /
                                 ServiceNow) → session waits or continues
                                 other parallel work
                deny           → structured rejection fed back to model
      after each turn: checkpoint (msg offset + fs diff), eval tick,
                       ledger append, budget check, compaction check
 5. Evaluator passes success criteria → model emits final report
 6. ResultEvent {status, artifacts[], cost, turns} → PR opened / files
    presented → session archived, resumable by id
```

**Event stream protocol** (the contract every client and the ADK consume — deliberately close to the Claude Agent SDK's message model so the ecosystem's mental model transfers):

```
SystemEvent      subtypes: init | compact_boundary | checkpoint |
                 policy_decision | suspended | resumed
PlanEvent        plan created/updated (full task ledger delta)
AssistantEvent   model text + tool-call requests for the turn
ToolEvent        tool started / progress / result / denied
ApprovalEvent    requested | granted | rejected | expired
EvalEvent        criterion checked {id, status, evidence}
ResultEvent      terminal: success | error_max_turns | error_budget |
                 error_execution | cancelled | needs_human
```

---

# 3. Agent Framework Design

The framework is **one model-driven loop with structured roles**, not five separate LLM services. Planner/Executor/Evaluator are *modes and artifacts* enforced by the harness — the same model instance (or a cheaper model for eval, configurable) operating under different structured-output contracts. This avoids the classic multi-LLM-service failure mode: context fragmentation and cross-service prompt drift.

## 3.1 Planner

**Output contract:** a versioned **Task Ledger** — the single structured source of truth for "what are we doing and how far along are we."

```json
{
  "goal": "Build a React application for Support Insights",
  "success_criteria": [
    {"id": "SC1", "text": "App builds with no errors (`npm run build`)", "check": "command", "cmd": "npm run build"},
    {"id": "SC2", "text": "Dashboard renders ticket-volume chart from API", "check": "test", "ref": "e2e/dashboard.spec.ts"},
    {"id": "SC3", "text": "Lint + typecheck clean", "check": "command", "cmd": "npm run lint && npx tsc --noEmit"}
  ],
  "tasks": [
    {"id": "T1", "title": "Scaffold Vite+React+TS project", "status": "done", "depends_on": []},
    {"id": "T2", "title": "Auth against Support Insights API", "status": "in_progress", "depends_on": ["T1"]},
    {"id": "T3", "title": "Ticket volume dashboard page", "status": "pending", "depends_on": ["T2"]}
  ],
  "assumptions": ["Support Insights API v2 per internal docs"],
  "open_questions": [],
  "version": 3
}
```

Design rules:

- **Machine-checkable success criteria are mandatory.** The planner must express at least one criterion as a command, test, or verifiable artifact. "Looks done" is not a stop condition. If a goal is inherently subjective ("investigate why ticket volume rose 30%"), the criterion becomes an artifact contract (e.g., "report at `findings.md` containing quantified root-cause analysis with data citations") checked by the Evaluator.
- **Plans are diffs, not rewrites.** Replanning emits a ledger delta with a stated reason — this keeps the audit trail meaningful and prevents plan thrash.
- **Plan depth is adaptive.** Trivial goals ("rename this function everywhere") skip formal planning (`plan_mode: lightweight`); the harness decides via a fast classifier on goal complexity, overridable by flag.
- **Clarify-then-commit.** If the goal is ambiguous, the planner's first action is the `AskUser` tool with pointed questions (never a wall of questions — max 3, with proposed defaults so headless mode can proceed on defaults).

## 3.2 Executor

The executor is the inner agentic loop: model proposes tool calls → harness executes → results return. Key mechanics:

- **Parallelism by effect type.** Read-only tools (Read, Grep, Glob, read-only MCP tools annotated `readOnlyHint`) execute concurrently; mutating tools (Edit, Write, Bash, git push) execute sequentially. Same rule the Claude Agent SDK uses; it's correct.
- **Structured tool errors.** Tool failures return typed errors (`ExitNonZero{code, stderr_tail}`, `Timeout`, `PolicyDenied{rule, remediation}`, `NotFound`) rather than raw text, so the model's recovery behavior is trainable and testable.
- **Working-notes discipline.** The executor maintains `NOTES.md` in the workspace (decisions, dead ends, file map). Survives compaction; becomes the seed context on resume.
- **Subagent delegation.** Executor can spawn scoped subagents (fresh context, restricted tool set, own budget slice) for isolatable subtasks — "write tests for module X", "research library Y". Only the subagent's structured summary returns to the parent, keeping parent context lean.

## 3.3 Evaluator / Critic

Runs at three cadences, cheapest first:

1. **Deterministic checks (every turn, free).** Run declared success-criteria commands/tests when relevant files changed; regressions surface immediately as ledger updates.
2. **Model critic (every K turns and before completion).** A separate model call — *without* the executor's conversational momentum, given only: goal, ledger, diff summary, latest test output. Answers a strict schema: `{on_track: bool, criteria_status[], drift_detected, wasted_effort, recommendation: continue|replan|escalate}`. Using a fresh-context critic is deliberate: the executor grading its own homework inside its own context is the primary source of agent overconfidence.
3. **Completion gate (before ResultEvent).** All machine-checkable criteria must pass; critic must return `on_track ∧ all criteria met`. If a criterion cannot be met, the agent must explicitly mark it `waived` with justification — which is surfaced to the human, never silently dropped.

## 3.4 Memory Manager

Four tiers, each with distinct lifetime and access rules:

| Tier | Contents | Lifetime | Backing |
|---|---|---|---|
| Working context | Message window | Current turn sequence | In-context |
| Session memory | Task ledger, NOTES.md, scratch KV | Session (survives compaction/resume) | Checkpoint store |
| Project memory | `BOB.md` conventions, learned repo facts (build quirks, flaky tests) — write-gated behind a `remember` tool with human-visible diffs | Persistent per repo | Repo file + project DB |
| Org memory | Approved patterns, golden solutions, tool docs | Persistent per org | Registry + vector index, read-only to agents, curated by admins |

Opinion: **agent-writable long-term memory is a governance surface.** Agents propose project-memory writes; they land as reviewable diffs (in-repo `BOB.md` changes go in the PR). No silent accumulation of "learned facts" — that's how agents ossify bad habits and how prompt-injection persists across sessions.

## 3.5 Context Manager

- Assembles each request as: system prompt → agent profile → project memory → task ledger (always fresh, never compacted) → conversation window → latest tool results.
- **Compaction** triggers at ~75% window: summarizes oldest history, pinning ledger, NOTES.md pointer, open file list, and last N turns verbatim. Emits `compact_boundary`; PreCompact hook can archive the full transcript.
- **Prompt-cache-aware ordering:** stable prefix (system, profile, project memory) first; per-tenant cache via Model Gateway. This is a 5–10x cost lever on long sessions — treat it as a P0 feature, not an optimization.
- **Tool-schema deferral:** only a core tool set loads upfront; MCP tool schemas load on demand via `ToolSearch` (same pattern as Claude's tool search). With enterprise catalogs of 100+ tools this is mandatory, not optional.

## 3.6 Tool Manager

- **Registry-backed.** Every tool (built-in, MCP, custom) is a registry entry: JSON-Schema I/O, effect class (`read | write | execute | network | irreversible`), risk tier, data-classification tags, owner, version.
- **Effect class drives everything:** parallelism (§3.2), policy defaults (§7), approval requirements, and sandbox privileges.
- **Uniform invocation:** the model never knows whether a tool is a local function, sandbox binary, or remote MCP server — the Tool Router resolves it. This makes tools swappable per environment (e.g., `deploy` = mock in dev, ArgoCD in prod).

---

# 4. Loop Engine Design

## 4.1 How `/loop` works — the canonical cycle

We adopt **Observe → Decide → Act → Evaluate** with an explicit outer/inner structure:

```
 /loop <goal> [flags]
      │
      ▼
 ┌─ INTAKE ─────────────────────────────────────────────┐
 │ parse flags → resolve agent profile → policy snapshot│
 │ → GoalSpec (clarify if ambiguous) → initial Plan     │
 └──────────────┬───────────────────────────────────────┘
                ▼
 ┌─ OUTER LOOP (per plan epoch) ────────────────────────┐
 │   ┌─ INNER LOOP (per turn) ────────────────────────┐ │
 │   │ OBSERVE  context assembly (ledger + results)   │ │
 │   │ DECIDE   model reasons → text and/or tool calls│ │
 │   │ ACT      policy check → execute → results back │ │
 │   │ TICK     checkpoint · ledger update · budget   │ │
 │   │          · deterministic evals · compaction    │ │
 │   └─────────────── repeat ────────────────────────┘  │
 │   every K turns / on trigger: CRITIC evaluation      │
 │   critic says replan? ──▶ REPLAN (ledger delta) ──┐  │
 └───────────────────────────────────────────────────┼──┘
                ▼                                    │
        STOP CONDITIONS  ◀───────────────────────────┘
        success (all criteria pass + critic gate)
        │ needs_human │ max_turns │ max_budget │ max_wallclock
        │ cancelled │ unrecoverable_error
                ▼
        RESULT: final report + artifacts + PR + ledger archive
```

Flags (defaults are the opinionated part):

```
/loop <goal>
  --agent bob-swe            # agent profile from registry
  --plan  auto|always|skip   # default auto; 'always' = show plan, wait for approval
  --autonomy supervised|auto|readonly   # default supervised (writes need approval
                                        # unless covered by allow rules)
  --max-turns 60  --max-budget 15.00  --max-hours 4
  --branch bob/loop-<id>     # NEVER works on the user's branch by default
  --resume <session_id> | --fork <session_id>@<turn>
```

## 4.2 Iteration lifecycle (one turn, precisely)

1. Context Manager assembles request (cache-stable prefix + fresh ledger + window).
2. Model responds: reasoning text + 0..N tool calls. Zero tool calls → candidate completion → jump to completion gate (§3.3.3).
3. For each tool call: Policy Engine verdict → `allow` (execute), `needs_approval` (park call; other approved parallel calls proceed; session enters `waiting_approval` only if nothing else can proceed), `deny` (typed rejection to model).
4. Tool results appended; ToolEvents streamed.
5. **Turn tick:** checkpoint (message offset + OverlayFS diff), ledger auto-update (task status inferred + model-declared), deterministic eval on touched criteria, budget/turn/wallclock check, compaction check.
6. Critic trigger check: every K=8 turns, or on trigger signals — 3 consecutive tool failures, same-file-edited-4-times, test-pass-count decreased, or model-declared uncertainty.

## 4.3 Success criteria

- Declared in the Task Ledger at plan time; typed: `command`, `test`, `artifact` (path + contract), `metric` (threshold), `human` (explicit sign-off item).
- Completion requires: every criterion `passed` or `waived`(justified, surfaced) **and** critic completion gate passes **and** required human sign-offs (per policy) granted.
- **Anti-gaming rule:** the agent may not edit success criteria after plan approval without emitting a `criteria_change` event that (in supervised mode) requires human ack. Agents deleting the failing test instead of fixing the bug is a real, observed failure mode; the harness must make it loud.

## 4.4 Failure handling

Typed failure taxonomy with distinct harness behavior:

| Failure | Detection | Harness response |
|---|---|---|
| Tool transient (network, 429) | Error type | Auto-retry w/ backoff, max 3; then surface to model |
| Tool deterministic (bad args, exit≠0) | Error type | Feed structured error to model (its job to fix) |
| Model stall / loop | N-gram repetition on actions; no ledger progress in M turns | Inject critic; if persists → forced replan; if persists → suspend `needs_human` |
| Regression | Deterministic evals: previously-passing criterion fails | Flag in ledger; critic forced next turn |
| Environment corruption | Sandbox health probe | Restore last-good checkpoint into fresh sandbox, replay ledger |
| Policy denial storm | ≥5 denials in a window | Suspend with summary: "agent needs permissions X,Y — approve or rescope" |
| Budget/turn/time cap | Counters | Graceful wind-down turn ("commit WIP, write status to NOTES.md") → `SUSPENDED`, resumable |
| Model refusal / safety stop | stop_reason | Surface verbatim to user; never auto-retry around a refusal |

## 4.5 Recovery mechanisms

- **Checkpoint/rewind:** every turn is a restore point (message log offset + fs OverlayFS layer). `bob loop rewind <id> --to-turn 12` restores state; `--fork` branches without touching the original — the debugging superpower none of the incumbents fully ship.
- **Resume:** suspended sessions restore ledger + NOTES.md + workspace snapshot; the first resumed turn is a forced re-orientation ("state of the world" summary) so the model doesn't act on stale beliefs.
- **Graceful degradation:** model gateway failover (primary → secondary model) mid-session is supported but always emits a visible event; silent model swaps are forbidden (they invalidate eval baselines).

## 4.6 Replanning strategies

Ordered by cost; the harness escalates:

1. **Tactical adjustment** (in-loop): model revises approach within current task. No ledger change.
2. **Ledger delta:** critic recommendation or model initiative → tasks added/split/reordered with reason recorded.
3. **Assumption invalidation:** a plan assumption proved false → affected tasks invalidated, partial re-plan of the dependent subtree only.
4. **Epoch reset:** approach is fundamentally wrong (critic: `recommendation=replan, severity=high`) → archive plan v_N, keep *learnings* section, restore workspace to last-good or clean checkpoint, plan v_N+1. Max 2 epoch resets per session before `needs_human`.
5. **Escalation:** `needs_human` with a decision brief: what was tried, why it failed, 2–3 proposed paths with cost estimates. Escalation quality is a first-class eval metric — an agent that escalates well is more valuable than one that thrashes autonomously.

---

# 5. SDK and ADK Design

**Principle:** the ADK is not a framework for building loops — the platform owns the loop. The ADK is how you (a) *drive* sessions programmatically, (b) *define* agents (profile, tools, policies, evals) as versionable artifacts, and (c) *extend* the platform with custom tools and hooks. This is the Claude-Agent-SDK-shaped bet, and it is the right one: developers who must hand-assemble plan/execute/critique graphs (LangGraph-style) ship worse agents slower.

## 5.1 Layered API

```
Layer 3  Agent definitions        bob.yaml manifest / @agent decorators
Layer 2  Session client           run(), stream events, approve, resume, fork
Layer 1  Extension points         @tool, hooks, custom evaluators, MCP servers
```

## 5.2 Python SDK

```python
import asyncio
from bob_adk import run, AgentOptions, tool, HookDecision
from bob_adk.events import PlanEvent, ToolEvent, ApprovalEvent, ResultEvent

# ---- Layer 1: a custom tool (auto-registered with schema from type hints)
@tool(
    effect="read",                      # read|write|execute|network|irreversible
    data_classes=["internal"],
    description="Query Support Insights ticket volume by product and window",
)
async def ticket_volume(product: str, days: int = 30) -> dict:
    return await si_client.volume(product=product, days=days)

# ---- Layer 1: a hook (runs in *your* process, outside model context)
async def block_prod_db(event) -> HookDecision:
    if event.tool == "bash" and "prod-db" in event.input.get("command", ""):
        return HookDecision.deny(reason="prod DB access requires change ticket")
    return HookDecision.allow()

# ---- Layer 2: drive a session
async def main():
    async for ev in run(
        goal="Investigate why ticket volume increased 30% this quarter; "
             "write findings.md with quantified root causes",
        options=AgentOptions(
            agent="bob-analyst",
            tools=["read", "bash(python *)", ticket_volume],   # allow rules
            hooks={"pre_tool_use": [block_prod_db]},
            autonomy="auto",
            max_turns=50, max_budget_usd=10.0,
            success_criteria=[{"type": "artifact", "path": "findings.md",
                               "contract": "root causes with data citations"}],
        ),
    ):
        match ev:
            case PlanEvent():      print("PLAN:", [t.title for t in ev.ledger.tasks])
            case ToolEvent(phase="result"): print(f"  {ev.tool} → {ev.summary}")
            case ApprovalEvent(phase="requested"):
                await ev.approve() if ev.risk_tier <= 2 else await ev.reject("too risky")
            case ResultEvent():
                print(ev.status, ev.result_text, f"${ev.total_cost_usd:.2f}")

asyncio.run(main())
```

## 5.3 TypeScript SDK

```typescript
import { run } from "@ibm/bob-adk";

for await (const ev of run({
  goal: "Create a Python SDK for the Support Insights internal API",
  options: {
    agent: "bob-swe",
    workspace: { repo: "github.ibm.com/support/si-sdk", branch: "bob/sdk-gen" },
    autonomy: "supervised",
    maxTurns: 80,
    onApprovalRequired: async (req) =>
      req.effect === "write" && req.path.startsWith("src/") ? "approve" : "ask_user",
  },
})) {
  if (ev.type === "assistant") ui.appendTurn(ev);
  if (ev.type === "result") console.log(ev.status, ev.sessionId);
}
```

## 5.4 Java SDK

Enterprise Java shops are an IBM reality; ship it in the first 90 days, not as an afterthought.

```java
BobClient bob = BobClient.builder().endpoint(BOB_ENDPOINT).auth(SsoAuth.fromEnv()).build();

SessionHandle session = bob.loop(LoopRequest.builder()
    .goal("Upgrade this service from Java 11 to 17; all tests green")
    .agent("bob-swe")
    .workspace(GitWorkspace.of("github.ibm.com/team/service", "bob/java17"))
    .autonomy(Autonomy.SUPERVISED)
    .maxTurns(100).maxBudgetUsd(new BigDecimal("25.00"))
    .build());

session.events().subscribe(ev -> {
    if (ev instanceof ApprovalEvent a && a.riskTier() <= RiskTier.LOW) a.approve();
    if (ev instanceof ResultEvent r) log.info("Done: {} ({} turns)", r.status(), r.numTurns());
});
```

## 5.5 Agent definition — the manifest is the product

An **agent** is a declarative, versioned, registry-published artifact. This is what makes agents governable, shareable, and testable:

```yaml
# bob.agent.yaml
apiVersion: bob/v1
kind: Agent
metadata:
  name: si-triage-agent
  owner: team-support-insights
  version: 1.4.0
spec:
  description: Triages Support Insights tickets, drafts fixes as PRs
  model:
    preferred: claude-sonnet          # via Model Gateway alias
    fallback: granite-4-code
    effort: high
  instructions: |
    You triage tickets for Support Insights. Always reproduce before fixing.
    Follow the conventions in BOB.md. Ship every change as a PR, never push main.
  tools:
    allow: ["read", "grep", "glob", "edit", "bash(npm *)", "bash(git *)",
            "mcp:github-enterprise/*", "mcp:jira/read_*"]
    deny:  ["bash(rm -rf *)", "mcp:jira/delete_*"]
  policy_profile: standard-swe        # org-managed, agent can only tighten
  limits: {max_turns: 60, max_budget_usd: 8.00, max_wallclock_hours: 2}
  subagents:
    - name: test-writer
      tools: {allow: ["read", "edit", "bash(npm test*)"]}
      effort: medium
  memory: {project: read_write_gated, org: read_only}
  evals: {suite: ./evals/triage.suite.yaml, gate_on_publish: true}
```

Registry lifecycle: `bob agent validate` → `bob agent test` (runs eval suite in sandbox) → `bob agent publish` (semver, provenance-signed) → available to `/loop --agent si-triage-agent`, headless triggers, and other agents (as subagents).

---

# 6. Tool Architecture

## 6.1 Built-in core (in every sandbox)

| Category | Tools | Notes |
|---|---|---|
| Filesystem | `read`, `write`, `edit` (anchored str-replace), `glob`, `grep`, `mkdir/mv/cp` | Edits are patch-based → every change is a reviewable diff and a checkpoint delta |
| Terminal | `bash` (streaming, timeout, background jobs, session-persistent shell) | Command-pattern allow rules: `bash(npm *)`; PTY support for interactive CLIs |
| Git | `git_status/diff/commit/branch/push`, `create_pr`, `pr_comment` | Push restricted to `bob/*` branches by default policy; commits trailer-signed `Co-authored-by: BOB` + session id |
| Build/Test | `run_tests` (framework auto-detect, structured pass/fail/coverage), `build`, `lint` | Structured results feed the Evaluator directly — this is why they're tools, not just bash |
| Meta | `ask_user`, `task_ledger_update`, `remember`, `spawn_subagent`, `tool_search` | |

## 6.2 IBM internal integrations (the moat)

Shipped as **first-party governed MCP servers** in the org catalog: GitHub Enterprise, Jenkins/Travis, Artifactory, Jira, ServiceNow (incidents + change requests — also the backend for approval workflows), Box, Slack, internal API catalogs, Instana/observability, and **Support Insights** itself (ticket queries, aggregates, customer-comms drafts — with PII-scrubbing middleware baked into the server, not left to the agent).

## 6.3 watsonx integrations

- **watsonx.ai** — Model Gateway backend: Granite models for cost-tiered work (critic passes, summarization, classification), hosted third-party models, on-prem serving for air-gapped tenants.
- **watsonx.governance** — session ledgers export as model-usage evidence; agent registry entries register as governed AI assets (this turns compliance from a blocker into a selling point).
- **watsonx Orchestrate** — bidirectional: Orchestrate skills callable as BOB tools; published BOB agents callable as Orchestrate skills. Positioning: Orchestrate owns business-workflow automation, BOB owns deep technical agents; the bridge prevents an internal turf war from confusing customers.
- **watsonx.data** — governed data access tool for analyst agents (query engines w/ row-level policy enforced server-side).

## 6.4 MCP compatibility

- BOB is an **MCP client**: any spec-compliant server attaches (stdio local, HTTP/SSE remote). Enterprise mode restricts attachment to registry-approved servers only.
- BOB is an **MCP server**: `bob-mcp` exposes `run_loop`, `get_session`, `approve` — so Claude Code, Cursor, or any MCP client can delegate work *to* BOB agents. Interop is a feature, not a threat.
- Registry wraps third-party MCP servers with governance metadata (risk tiers per tool, data classes, egress domains) that the raw MCP spec lacks.

---

# 7. Enterprise Requirements

## 7.1 Security

- **Sandbox isolation:** gVisor default; Kata (microVM) for regulated tenants; per-session ephemeral workspaces; no cross-session filesystem visibility.
- **Egress control:** default-deny network from sandbox; allowlist derived from tool registry entries (a Jira tool grants Jira's domains only). Prompt-injection blast-radius control is *architectural*: even a fully hijacked agent cannot exfiltrate to arbitrary hosts.
- **Scoped, short-lived credentials:** agents never see raw secrets. Tool Router injects credentials server-side at call time (Vault-issued, TTL-bound, scoped to the tool + session identity). Nothing secret ever enters model context.
- **Untrusted-content tainting:** tool results from external content (web, tickets, emails) are tagged; policy escalates approval requirements for mutating actions in tainted turns ("agent read an external ticket, now wants to `git push`" → approval, even in auto mode).
- **Model gateway hygiene:** per-tenant routing, optional PII scrubbing, no training on tenant data, regional pinning.

## 7.2 Governance & policy

Single policy engine (OPA/Rego) evaluated on **every tool call** with input `{identity, agent, tool, args, effect, data_classes, environment, taint, session}` → verdict `allow | deny | needs_approval(approvers, ttl) | allow_with(constraints)`.

- Policies are hierarchical: org → team → agent profile → session flags, where lower layers may only **tighten**.
- Policy versions are pinned per session at start (mid-session policy changes create a visible `policy_decision` boundary event, they don't silently reinterpret history).
- Shipped profiles: `readonly`, `standard-swe`, `standard-analyst`, `ci-autonomous`, `regulated` — so day-one adoption doesn't require writing Rego.

## 7.3 Auditability & compliance

- The **event ledger is the audit log**: append-only, hash-chained, exportable (SIEM/QRadar), with every model request/response digest, tool call + policy verdict + approver identity, and fs diff reference. A session can be **replayed** end-to-end from the ledger — this single property satisfies most auditor conversations.
- Compliance mappings shipped as documentation + automated evidence packs: SOC 2, ISO 27001, and (roadmap) FedRAMP-deployable topology. EU AI Act posture: agents are registered assets with documented capabilities, human-oversight modes, and logging — via the watsonx.governance bridge.
- Retention: configurable per tenant; ledgers separate from workspace artifacts (code lives in git; the ledger references commits).

## 7.4 Access control

RBAC on: running agents (per agent profile), approving (per risk tier — approvers of tier-3 actions must hold role X), publishing agents, editing policies, viewing sessions (team-scoped by default). Agent identity is **derived, never ambient**: a session runs as `bob-agent/<agent>@<user>` — the union-capped intersection of what the agent profile allows and what the invoking human is entitled to. An agent can never do what its invoker couldn't.

## 7.5 Guardrails & approval workflows

- Effect-class defaults: `read` auto; `write` auto within workspace, approval outside; `execute` allow-rule gated; `network` registry-domain gated; `irreversible` (deploy, delete, external comms, payments) **always** requires approval — not overridable below org level.
- Approval channels: inline (attached client), Slack/Teams interactive message, ServiceNow change request (for `irreversible` in regulated profiles), email fallback. Approvals carry TTL; expiry suspends the session (resumable), never auto-approves.
- **Batch approvals with plan-level grants:** approving the plan can pre-grant enumerated capabilities ("edits under `src/`, `npm test`") so supervised mode isn't a click-storm. This is the difference between governance people tolerate and governance people bypass.

---

# 8. Autonomous Agent Capabilities

- **Self-correction** is structural, not aspirational: typed tool errors (§3.2), deterministic eval regression flags (§4.4), stall detection with forced critic, and checkpoint-restore give the model both the *signal* and the *mechanism* to correct. We do not rely on "the model will notice."
- **Reflection:** the fresh-context critic (§3.3) plus a mandatory end-of-session retrospective appended to the ledger (`what worked / what wasted turns`) — mined offline to improve agent instructions and org memory (human-curated, per §3.4).
- **Multi-step reasoning:** extended thinking enabled for planning/replanning/critic turns; effort tiering per phase (high for plan/debug, medium for routine execution, low for mechanical steps) — a direct cost lever.
- **Long-running workflows:** sessions are durable state machines (suspend/resume/checkpoint), so "run overnight," "wait 40 min for CI," and "wait 2 days for approval" are the same mechanism: suspend on a wait-condition, resume on webhook/timer. Wall-clock caps prevent zombie sessions.
- **Multi-agent collaboration:** v1 pattern is **hierarchical** — orchestrator + scoped subagents with isolated contexts, restricted tools, budget slices, and structured-summary returns. Parallel subagents supported for independent subtrees (each in its own workspace branch; orchestrator merges). Peer-to-peer swarms are explicitly out of scope for v1: they demo well and audit terribly.
- **Agent-to-agent communication:** typed contracts only — `TaskRequest{goal, inputs, criteria, budget} → TaskResult{status, artifacts, summary}` over the registry (a published agent is callable like a tool). Support Google's A2A protocol at the boundary for cross-vendor interop; internally, the ledger records every inter-agent call like any tool call, preserving one audit spine.

---

# 9. Developer Experience

## 9.1 CLI

```
bob loop "fix flaky tests in payments module" --autonomy supervised
bob loop status | attach <id> | pause <id> | resume <id> --max-turns +20
bob loop fork <id> --at-turn 12 "try the mock-based approach instead"
bob loop diff <id>            # everything this session changed
bob loop replay <id> [--step] # deterministic ledger replay (debugging/audit)
bob agent init|validate|test|publish
bob tools list|search "jira"
bob policy explain <session_id> <event_id>   # why was this call denied?
```

Live TUI: streaming turns, collapsible tool outputs, plan sidebar with task/criteria status, inline y/n/always-allow approvals, type-to-steer at any time.

## 9.2 Local dev, testing, debugging

- `bob dev` — full local stack (embedded control plane, local sandbox, mock approval channel). Hot-reload of agent manifest and custom tools.
- **Testing framework (`bob-adk.testing`):** three levels — unit (custom tools as plain functions), scenario (scripted model responses to test hooks/policies deterministically, no LLM cost), and **eval suites**: golden tasks in containerized fixtures with graders (`tests_pass`, `artifact_contract`, `llm_rubric`, `cost_under`), run N seeds for variance. `gate_on_publish: true` makes evals the agent's CI. This is a genuine differentiator — none of the incumbent coding agents give *users* an eval harness for *their own* agents.
- **Observability:** OpenTelemetry-native — one trace per session, spans per turn/tool/model call, standard attributes (`bob.session_id`, `bob.turn`, `bob.tool.effect`); dashboards for cost, success rate, turns-to-success, approval latency, denial hotspots.
- **Debugging:** time-travel replay from the ledger, fork-at-turn for counterfactuals ("what if I'd approved that?"), `policy explain`, and context inspection (`bob loop context <id> --turn 14` shows exactly what the model saw — the #1 question when debugging agents).

---

# 10. Competitive Analysis

| Dimension | **BOB /loop (this design)** | Cursor Agent | Claude Code | OpenAI Codex | Replit Agent | LangGraph | CrewAI |
|---|---|---|---|---|---|---|---|
| Loop quality | Model-driven loop, critic, typed evals | Strong, IDE-centric | **Benchmark**; SDK-embeddable | Strong; cloud sandbox parallel tasks | Strong within Replit | You build it | Role-based, shallow loop |
| Enterprise policy engine | **Per-call OPA, effect classes, taint tracking** | Basic | Permission rules/modes, hooks | Org controls, limited | Minimal | DIY | DIY |
| Audit/replay | **Hash-chained ledger, full replay, fork-at-turn** | No | Transcripts, hooks | Partial logs | No | DIY (checkpointing exists) | No |
| Approval workflows | **ServiceNow/Slack native, plan-level grants** | Inline only | Inline callback | Inline | Inline | DIY | No |
| Custom agents (ADK) | Manifest + registry + eval gating, 3 languages | No | Agent SDK (strong; Py/TS) | AgentKit (early) | No | **Core strength** | Core strength |
| Enterprise tool estate | **IBM catalog + governed MCP + watsonx** | MCP | MCP | Connectors | Replit-centric | Any (DIY) | Any (DIY) |
| Long-running/durable | Suspend/resume/webhooks, days-long sessions | Session-bound | Session + resume | Cloud tasks (good) | Session-bound | **Durable graphs (good)** | Weak |
| On-prem/air-gap | **Yes (watsonx.ai serving)** | No | No (API-bound) | No | No | Self-host (BYO model) | Self-host |
| Model choice | Gateway: Anthropic/OpenAI/Granite/on-prem | Multi | Anthropic | OpenAI | Fixed | Any | Any |

**Honest read:** we will not out-loop Claude Code in year one — its harness benefits from co-training with the model. We win where they structurally can't follow: policy-per-tool-call, replayable audit, approval integration with enterprise change management, on-prem models, governed agent registry with eval gating, and Java. Against LangGraph/CrewAI: they are libraries, we are a *platform* — they give you graph primitives and leave sandboxing, policy, audit, approvals, and ops to you; that gap is precisely our product.

**Table stakes we must not fumble:** loop quality and latency. If `/loop` feels dumber than Cursor on a laptop, no amount of governance saves it. Hence: best available frontier models via gateway (not Granite-only), prompt caching from day one, and relentless eval benchmarking against SWE-bench-style suites plus internal task suites.

---

# 11. MVP Roadmap

## 30 days — "The loop works"
- `loop-core` engine: turn loop, tool router, built-in fs/terminal/git/test tools, task ledger, deterministic evals, checkpointing (message log + fs diff), max turns/budget.
- CLI `/loop` with streaming TUI, supervised approvals inline, `bob/` branch discipline, PR output.
- Local sandbox (container), model gateway v0 (Anthropic + watsonx.ai, prompt caching).
- Internal dogfood: 20 developers, 5 golden tasks, success-rate + turns + cost baselines.
- **Exit criteria:** ≥60% unassisted success on the golden suite; median simple-task session < 8 min.

## 90 days — "It's a platform"
- Python + TypeScript ADK (run/stream/hooks/custom tools); agent manifests + registry v1; eval harness with publish gating.
- Control plane service: remote sessions, suspend/resume, Slack approvals; OPA policy engine with shipped profiles; ledger + OTel.
- MCP client support (registry-approved servers); first IBM tool pack: GitHub Enterprise, Jira, Jenkins, Slack.
- Critic evaluator + replanning epochs; subagents (hierarchical).
- **Exit criteria:** 3 internal teams shipping custom agents; ≥75% golden-suite success; first headless (CI-triggered) agent in production use.

## 6 months — "Enterprise GA"
- Java SDK; ServiceNow approval + change-request integration; watsonx.governance export; SIEM export; Kata high-isolation tier; regional/on-prem deployment topology.
- Fork/rewind/replay debugging suite; org memory + curated pattern library; Support Insights flagship agents in production (§12).
- A2A boundary support; BOB-as-MCP-server; marketplace-ready registry (internal).
- **Exit criteria:** one external design-partner enterprise live; audit replay demoed to a real compliance team; ≥85% golden-suite success; p50 cost/task down 40% vs. day-30 via caching + effort tiering.

## Team (steady state, ~24 engineers)
Loop engine & context 5 · Sandbox/infra 4 · Control plane (policy/audit/approvals) 5 · ADK & DX 4 · Tools/integrations 3 · Evals/quality 2 · PM/Design 1+1. Critical hire profile: engineers who have *shipped* an agent harness, not just used one.

## Technical risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Loop quality below competitors (model + harness tuning gap) | High | Fatal | Frontier models via gateway; continuous eval benchmarking; steal-shamelessly harness patterns (compaction, tool schemas, subagents); dedicated evals team from day 1 |
| Governance friction kills adoption (approval fatigue) | High | High | Plan-level grants, always-allow rules, risk-tiered defaults; measure approval latency as a product KPI |
| Sandbox performance (build toolchains in gVisor) | Medium | Medium | Warm pool of pre-baked toolchain images; `--trust-workspace` local escape hatch for dev machines |
| Prompt injection via enterprise content (tickets, docs) | High | High | Taint tracking + escalated approvals, egress allowlists, no raw secrets in context — assume compromise, cap blast radius |
| Cost blowout on long sessions | Medium | Medium | Prompt caching P0, effort tiering, Granite for critic/summarization, hard budget caps with graceful suspend |
| Scope creep toward workflow platform | Medium | High | Non-goals enforced; Orchestrate bridge instead of rebuild |
| Model vendor dependency | Medium | Medium | Gateway abstraction + eval parity tracking across models from day 1 |

---

# 12. Future Vision

- **Enterprise agent platform:** the registry becomes the org's agent estate — every agent versioned, eval-gated, policy-bound, cost-attributed; dashboards answer "what did our agents do this quarter, at what cost, with what success rate" — the FinOps + governance view no competitor offers.
- **Agent marketplace:** internal first (teams publish, others subscribe, with provenance signing and eval scores as the trust signal), then IBM-curated partner catalog (SAP migration agents, mainframe modernization agents, security-remediation agents) with revenue share. The manifest + eval-gate + signing pipeline built in §5.5 *is* the marketplace substrate.
- **Internal IBM ecosystem:** BOB agents as the execution layer under watsonx Orchestrate's business workflows; consulting (IBM CIC) packaging domain agents as delivery accelerators; every internal tool team shipping a governed MCP server as standard practice.
- **Support Insights use cases:** auto-triage agent (reproduce → root-cause → draft fix PR → draft customer comms for human send); ticket-trend analyst on a weekly timer (`/loop` headless) producing quantified drill-downs; knowledge-gap agent that mines resolved tickets into doc PRs.
- **Operations:** incident-response copilot (Instana alert → evidence gathering → mitigation proposal → gated runbook execution); patch/CVE remediation fleets (one agent per repo, parallel, each shipping a PR); cost-optimization analyst over cloud billing data.
- **Development:** dependency-upgrade fleets, test-coverage raisers, framework-migration campaigns (Java 8→17 across 400 repos as a *campaign* object: one agent template, per-repo sessions, aggregate dashboard) — the "fleet of loops" pattern is where platform economics beat per-seat IDE agents decisively.

---

## Appendix A — Key design decisions (for the review meeting)

1. **Model-driven loop over graph orchestration** — argued in Exec Summary; the graph layer is optional ADK sugar, not the core.
2. **Task Ledger as the spine** — plan, progress, criteria, and audit narrative in one structured, versioned artifact.
3. **Fresh-context critic** — separate evaluation call, cheap model allowed; the executor never grades itself in-context.
4. **Effect classes on every tool** — one taxonomy drives parallelism, policy, approvals, and sandbox privileges.
5. **Ledger = audit log = replay source = eval substrate** — one event stream, four consumers, zero drift.
6. **Derived agent identity** — agent ∩ invoker entitlements; no ambient service-account superpowers.
7. **Suspend-not-kill** — every limit and wait condition produces a resumable checkpoint.
8. **Evals as publish gate** — an agent without a passing eval suite cannot ship to the registry.
