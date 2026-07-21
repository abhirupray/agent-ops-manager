"""
Persistence layer (repository pattern) for all supervisor state.

Why this exists (the production audit's #1 finding): supervisor state --
agent profiles, pending approvals, results -- previously lived in process
memory. Two consequences: (a) restart = amnesia for everything except the
audit log, and (b) split-brain: the API process and the dashboard process
each held their own supervisor with divergent state.

Design decisions, stated for the record:
- SQLite, not Postgres: single-node platform, zero-ops persistence, and the
  repository interface below is the seam where Postgres would slot in --
  swapping engines changes this file only, nothing above it.
- Repository pattern, not an ORM: the queries are simple, the schema is
  small, and SQLAlchemy would add a dependency and a layer of indirection
  for no current benefit. At Postgres-scale multi-writer concurrency, an
  ORM + connection pooling becomes worth it; that trade-off is documented,
  not ignored.
- Write-through, not write-behind: every mutation is persisted immediately
  inside the mutating call. Simple, crash-safe, and correct at this
  throughput. A high-QPS system would batch; this is not that system.
"""
import json
import sqlite3
from contextlib import contextmanager
from typing import Optional

from .models import AgentProfile, AutonomyLevel, Task, TaskResult, TaskRisk, TaskStatus


class StateStore:
    def __init__(self, db_path: str = "agent_ops.db"):
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _connect(self):
        """Yields a connection and guarantees it's closed on exit, commit or
        not. Plain `with sqlite3.connect(...) as conn:` only commits/rolls
        back on exit -- it does NOT close the connection, which leaks file
        handles and shows up as 'file in use by another process' errors on
        Windows. This wrapper is the fix, in one place."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    allowed_task_types TEXT NOT NULL,
                    autonomy_level INTEGER NOT NULL,
                    wip_limit INTEGER NOT NULL,
                    is_paused INTEGER NOT NULL DEFAULT 0,
                    rolling_quality_scores TEXT NOT NULL DEFAULT '[]',
                    completed_task_count INTEGER NOT NULL DEFAULT 0,
                    active_task_ids TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_tasks (
                    task_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    task_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_results (
                    task_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    output TEXT,
                    quality_score REAL,
                    quality_reasoning TEXT,
                    duration_seconds REAL,
                    completed_at TEXT
                )
                """
            )

    # -- Agents -----------------------------------------------------------------

    def save_agent(self, agent: AgentProfile) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agents (agent_id, role, allowed_task_types, autonomy_level, wip_limit,
                                    is_paused, rolling_quality_scores, completed_task_count, active_task_ids)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    role=excluded.role,
                    allowed_task_types=excluded.allowed_task_types,
                    autonomy_level=excluded.autonomy_level,
                    wip_limit=excluded.wip_limit,
                    is_paused=excluded.is_paused,
                    rolling_quality_scores=excluded.rolling_quality_scores,
                    completed_task_count=excluded.completed_task_count,
                    active_task_ids=excluded.active_task_ids
                """,
                (
                    agent.agent_id, agent.role, json.dumps(agent.allowed_task_types),
                    int(agent.autonomy_level), agent.wip_limit, int(agent.is_paused),
                    json.dumps(agent.rolling_quality_scores), agent.completed_task_count,
                    json.dumps(agent.active_task_ids),
                ),
            )

    def load_agent(self, agent_id: str) -> Optional[AgentProfile]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if row is None:
            return None
        return AgentProfile(
            agent_id=row["agent_id"],
            role=row["role"],
            allowed_task_types=json.loads(row["allowed_task_types"]),
            autonomy_level=AutonomyLevel(row["autonomy_level"]),
            wip_limit=row["wip_limit"],
            is_paused=bool(row["is_paused"]),
            rolling_quality_scores=json.loads(row["rolling_quality_scores"]),
            completed_task_count=row["completed_task_count"],
            active_task_ids=json.loads(row["active_task_ids"]),
        )

    # -- Pending approvals -------------------------------------------------------

    def save_pending(self, task: Task, agent_id: str) -> None:
        task_json = json.dumps({
            "task_type": task.task_type, "description": task.description,
            "definition_of_done": task.definition_of_done, "payload": task.payload,
            "risk": task.risk.value, "task_id": task.task_id, "created_at": task.created_at,
        })
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO pending_tasks (task_id, agent_id, task_json) VALUES (?, ?, ?)",
                (task.task_id, agent_id, task_json),
            )

    def delete_pending(self, task_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM pending_tasks WHERE task_id = ?", (task_id,))

    def load_all_pending(self) -> list[tuple[Task, str]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM pending_tasks").fetchall()
        out = []
        for row in rows:
            d = json.loads(row["task_json"])
            d["risk"] = TaskRisk(d["risk"])
            out.append((Task(**d), row["agent_id"]))
        return out

    # -- Results -----------------------------------------------------------------

    def save_result(self, result: TaskResult) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO task_results
                    (task_id, agent_id, status, output, quality_score, quality_reasoning,
                     duration_seconds, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.task_id, result.agent_id, result.status.value,
                    str(result.output) if result.output is not None else None,
                    result.quality_score, result.quality_reasoning,
                    result.duration_seconds, result.completed_at,
                ),
            )

    def load_result(self, task_id: str) -> Optional[TaskResult]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM task_results WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        return TaskResult(
            task_id=row["task_id"], agent_id=row["agent_id"], status=TaskStatus(row["status"]),
            output=row["output"], quality_score=row["quality_score"],
            quality_reasoning=row["quality_reasoning"], duration_seconds=row["duration_seconds"],
            completed_at=row["completed_at"],
        )
