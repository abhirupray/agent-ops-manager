"""
Audit trail: every decision the supervisor makes gets logged here, append-
only. This is the "black box flight recorder" piece -- the thing that lets
someone answer "why did this agent do that?" after the fact, which is
exactly the gap named in the 2026 enterprise AI governance research this
project is built around (see README).
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional


class AuditLog:
    def __init__(self, db_path: str = "agent_ops.db"):
        self.db_path = db_path
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    agent_id TEXT,
                    task_id TEXT,
                    event_type TEXT NOT NULL,
                    details TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def log(self, event_type: str, agent_id: Optional[str] = None,
             task_id: Optional[str] = None, details: Optional[dict] = None):
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO audit_log (timestamp, agent_id, task_id, event_type, details) VALUES (?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    agent_id,
                    task_id,
                    event_type,
                    json.dumps(details or {}),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def query(self, agent_id: Optional[str] = None, task_id: Optional[str] = None, limit: int = 200) -> list[dict]:
        clauses, params = [], []
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?", params + [limit]
            ).fetchall()
        finally:
            conn.close()

        return [
            {**dict(r), "details": json.loads(r["details"])} for r in rows
        ]
