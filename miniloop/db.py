import sqlite3
import json
from datetime import datetime, timezone
from typing import Optional
from miniloop.config import DB_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id              TEXT PRIMARY KEY,
                goal            TEXT,
                status          TEXT,
                workspace_path  TEXT,
                max_turns       INT,
                max_budget_usd  REAL,
                cost_usd        REAL DEFAULT 0,
                turn_count      INT  DEFAULT 0,
                model           TEXT,
                created_at      TEXT,
                updated_at      TEXT
            );

            CREATE TABLE IF NOT EXISTS events (
                seq         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT,
                turn        INT,
                type        TEXT,
                payload     TEXT,
                created_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id           TEXT,
                session_id   TEXT,
                title        TEXT,
                status       TEXT,
                depends_on   TEXT,
                plan_version INT,
                PRIMARY KEY (id, session_id)
            );

            CREATE TABLE IF NOT EXISTS criteria (
                id           TEXT,
                session_id   TEXT,
                description  TEXT,
                check_cmd    TEXT,
                status       TEXT,
                last_output  TEXT,
                PRIMARY KEY (id, session_id)
            );

            CREATE TABLE IF NOT EXISTS tool_calls (
                id          TEXT PRIMARY KEY,
                session_id  TEXT,
                turn        INT,
                tool_name   TEXT,
                args        TEXT,
                effect      TEXT,
                verdict     TEXT,
                result      TEXT,
                duration_ms INT
            );

            CREATE TABLE IF NOT EXISTS approvals (
                tool_call_id  TEXT PRIMARY KEY,
                decision      TEXT,
                decided_by    TEXT,
                decided_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS checkpoints (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id      TEXT,
                turn            INT,
                last_event_seq  INT,
                messages_json   TEXT,
                created_at      TEXT
            );
        """)


# ── sessions ─────────────────────────────────────────────────────────────────

def insert_session(session_id: str, goal: str, workspace_path: str,
                   max_turns: int, max_budget_usd: float, model: str) -> None:
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO sessions
               (id, goal, status, workspace_path, max_turns, max_budget_usd,
                cost_usd, turn_count, model, created_at, updated_at)
               VALUES (?,?,?,?,?,?,0,0,?,?,?)""",
            (session_id, goal, "running", workspace_path,
             max_turns, max_budget_usd, model, now, now),
        )


def get_session(session_id: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()


def update_session(session_id: str, **kwargs) -> None:
    kwargs["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in kwargs)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE sessions SET {cols} WHERE id = ?",
            (*kwargs.values(), session_id),
        )


# ── events ────────────────────────────────────────────────────────────────────

def insert_event(session_id: str, turn: int, type_: str, payload: dict) -> int:
    now = _now()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO events (session_id, turn, type, payload, created_at) VALUES (?,?,?,?,?)",
            (session_id, turn, type_, json.dumps(payload), now),
        )
        return cur.lastrowid


def get_events(session_id: str) -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM events WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()


def max_event_seq(session_id: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(seq) FROM events WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row[0] or 0


# ── tasks ─────────────────────────────────────────────────────────────────────

def upsert_task(session_id: str, task_id: str, title: str, status: str,
                depends_on: list, plan_version: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO tasks (id, session_id, title, status, depends_on, plan_version)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(id, session_id) DO UPDATE SET
                 title=excluded.title, status=excluded.status,
                 depends_on=excluded.depends_on, plan_version=excluded.plan_version""",
            (task_id, session_id, title, status, json.dumps(depends_on), plan_version),
        )


def get_tasks(session_id: str) -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE session_id = ?", (session_id,)
        ).fetchall()


# ── criteria ──────────────────────────────────────────────────────────────────

def upsert_criterion(session_id: str, crit_id: str, description: str,
                     check_cmd: str, status: str = "pending",
                     last_output: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO criteria
               (id, session_id, description, check_cmd, status, last_output)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(id, session_id) DO UPDATE SET
                 description=excluded.description, check_cmd=excluded.check_cmd,
                 status=excluded.status, last_output=excluded.last_output""",
            (crit_id, session_id, description, check_cmd, status, last_output),
        )


def get_criteria(session_id: str) -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM criteria WHERE session_id = ?", (session_id,)
        ).fetchall()


# ── tool_calls ────────────────────────────────────────────────────────────────

def insert_tool_call(call_id: str, session_id: str, turn: int, tool_name: str,
                     args: dict, effect: str, verdict: str,
                     result: dict, duration_ms: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO tool_calls
               (id, session_id, turn, tool_name, args, effect, verdict, result, duration_ms)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (call_id, session_id, turn, tool_name,
             json.dumps(args), effect, verdict, json.dumps(result), duration_ms),
        )


# ── approvals ─────────────────────────────────────────────────────────────────

def insert_approval(tool_call_id: str, decision: str, decided_by: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO approvals (tool_call_id, decision, decided_by, decided_at)
               VALUES (?,?,?,?)""",
            (tool_call_id, decision, decided_by, _now()),
        )


# ── checkpoints ───────────────────────────────────────────────────────────────

def insert_checkpoint(session_id: str, turn: int,
                      last_event_seq: int, messages: list) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO checkpoints
               (session_id, turn, last_event_seq, messages_json, created_at)
               VALUES (?,?,?,?,?)""",
            (session_id, turn, last_event_seq, json.dumps(messages), _now()),
        )


def get_latest_checkpoint(session_id: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM checkpoints WHERE session_id = ?
               ORDER BY id DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
