from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Session:
    id: str
    goal: str
    status: str
    workspace_path: str
    max_turns: int
    max_budget_usd: float
    cost_usd: float
    turn_count: int
    model: str
    created_at: str
    updated_at: str


@dataclass
class Event:
    seq: int
    session_id: str
    turn: int
    type: str       # system|plan|assistant|tool|eval|approval|result
    payload: str    # JSON
    created_at: str


@dataclass
class Task:
    id: str
    session_id: str
    title: str
    status: str     # pending|in_progress|done|blocked|abandoned
    depends_on: str # JSON list of task ids
    plan_version: int


@dataclass
class Criterion:
    id: str
    session_id: str
    description: str
    check_cmd: str  # shell command; exit 0 = pass
    status: str     # pending|passed|failed|waived
    last_output: str


@dataclass
class ToolCall:
    id: str
    session_id: str
    turn: int
    tool_name: str
    args: str           # JSON
    effect: str         # read|write|execute|network|irreversible
    verdict: str        # allowed|approved|denied
    result: str         # JSON: ok, output, error
    duration_ms: int


@dataclass
class Approval:
    tool_call_id: str
    decision: str       # approved|denied
    decided_by: str
    decided_at: str


@dataclass
class Checkpoint:
    id: int
    session_id: str
    turn: int
    last_event_seq: int
    messages_json: str
    created_at: str
