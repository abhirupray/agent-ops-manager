import importlib
import os
import sys


import pytest
from fastapi.testclient import TestClient

from src.core.state_store import StateStore
from src.core import AgentProfile, AutonomyLevel, AuditLog, Supervisor, Task
from src.integrations.demo_worker import ReliableDemoWorker


def make_task() -> Task:
    return Task(task_type="demo_task", description="Do a thing", definition_of_done="The thing is done.")


# -- Persistence: the split-brain / restart-amnesia fix ---------------------------

def test_agent_state_survives_supervisor_restart(tmp_path):
    """The production audit's #1 finding: trust, autonomy, and pause state must
    survive process restarts. Simulates a restart by building a second
    Supervisor over the same database files."""
    audit_db, state_db = str(tmp_path / "a.db"), str(tmp_path / "s.db")

    sup1 = Supervisor(audit_log=AuditLog(audit_db), state_store=StateStore(state_db))
    sup1.register_agent(
        AgentProfile("agent-1", "Tester", ["demo_task"], AutonomyLevel.L0_APPROVE_EVERY_ACTION),
        ReliableDemoWorker(),
    )
    # Earn a promotion (5 approved tasks) and then pause the agent
    for _ in range(5):
        task = make_task()
        sup1.assign_task("agent-1", task)
        sup1.approve_task(task.task_id)
    sup1.pause_agent("agent-1")
    assert sup1.agents["agent-1"].autonomy_level == AutonomyLevel.L1_APPROVE_HIGH_RISK

    # "Restart": a brand new process would re-run registration with code defaults.
    sup2 = Supervisor(audit_log=AuditLog(audit_db), state_store=StateStore(state_db))
    sup2.register_agent(
        AgentProfile("agent-1", "Tester", ["demo_task"], AutonomyLevel.L0_APPROVE_EVERY_ACTION),
        ReliableDemoWorker(),
    )
    hydrated = sup2.agents["agent-1"]
    assert hydrated.autonomy_level == AutonomyLevel.L1_APPROVE_HIGH_RISK  # earned trust survived
    assert hydrated.is_paused is True                                      # kill switch survived
    assert hydrated.completed_task_count == 5


def test_pending_approvals_survive_restart(tmp_path):
    audit_db, state_db = str(tmp_path / "a.db"), str(tmp_path / "s.db")
    sup1 = Supervisor(audit_log=AuditLog(audit_db), state_store=StateStore(state_db))
    sup1.register_agent(
        AgentProfile("agent-1", "Tester", ["demo_task"], AutonomyLevel.L0_APPROVE_EVERY_ACTION),
        ReliableDemoWorker(),
    )
    task = make_task()
    sup1.assign_task("agent-1", task)  # goes to pending at L0

    sup2 = Supervisor(audit_log=AuditLog(audit_db), state_store=StateStore(state_db))
    sup2.register_agent(
        AgentProfile("agent-1", "Tester", ["demo_task"], AutonomyLevel.L0_APPROVE_EVERY_ACTION),
        ReliableDemoWorker(),
    )
    assert task.task_id in sup2.pending_approvals  # approval queue survived the restart
    result = sup2.approve_task(task.task_id)        # and is actionable in the new process
    assert result.status.value == "COMPLETED"


def test_feedback_works_on_persisted_result_after_restart(tmp_path):
    audit_db, state_db = str(tmp_path / "a.db"), str(tmp_path / "s.db")
    sup1 = Supervisor(audit_log=AuditLog(audit_db), state_store=StateStore(state_db))
    sup1.register_agent(
        AgentProfile("agent-1", "Tester", ["demo_task"], AutonomyLevel.L4_FULLY_AUTONOMOUS),
        ReliableDemoWorker(),
    )
    task = make_task()
    sup1.assign_task("agent-1", task)

    sup2 = Supervisor(audit_log=AuditLog(audit_db), state_store=StateStore(state_db))
    sup2.register_agent(
        AgentProfile("agent-1", "Tester", ["demo_task"], AutonomyLevel.L4_FULLY_AUTONOMOUS),
        ReliableDemoWorker(),
    )
    # Result was created in "process 1"; feedback lands in "process 2" via persistence.
    sup2.record_human_feedback(task.task_id, corrected_score=0.1, note="Wrong.")
    assert 0.1 in sup2.agents["agent-1"].rolling_quality_scores


# -- API auth / RBAC ---------------------------------------------------------------

@pytest.fixture
def client_with_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OPS_ADMIN_KEY", "test-admin-key")
    monkeypatch.setenv("AGENT_OPS_REVIEWER_KEY", "test-reviewer-key")
    monkeypatch.setenv("AUDIT_DB_PATH", str(tmp_path / "api_audit.db"))
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "api_state.db"))
    # Reset the bootstrap singleton so this test gets a fresh supervisor
    import src.bootstrap as bootstrap
    bootstrap._supervisor = None
    from src.api import main as api_main
    importlib.reload(api_main)
    yield TestClient(api_main.app)
    bootstrap._supervisor = None


def test_missing_key_rejected(client_with_auth):
    assert client_with_auth.get("/agents").status_code == 401


def test_invalid_key_rejected(client_with_auth):
    r = client_with_auth.get("/agents", headers={"X-API-Key": "wrong-key"})
    assert r.status_code == 401


def test_reviewer_can_read_but_not_pause(client_with_auth):
    headers = {"X-API-Key": "test-reviewer-key"}
    assert client_with_auth.get("/agents", headers=headers).status_code == 200
    r = client_with_auth.post("/agents/demo-reliable-agent/pause", headers=headers)
    assert r.status_code == 403  # RBAC: reviewers cannot use the kill switch


def test_admin_can_pause_and_health_is_public(client_with_auth):
    assert client_with_auth.get("/health").status_code == 200  # unauthenticated
    r = client_with_auth.post("/agents/demo-reliable-agent/pause",
                                headers={"X-API-Key": "test-admin-key"})
    assert r.status_code == 200
    assert r.json()["is_paused"] is True


def test_assign_without_agent_id_routes_by_trust(client_with_auth):
    headers = {"X-API-Key": "test-admin-key"}
    # Resume first (previous test may have paused within same fixture scope is fresh anyway)
    body = {"task_type": "demo_task", "description": "route me",
            "definition_of_done": "done", "risk": "LOW"}
    r = client_with_auth.post("/tasks/assign", json=body, headers=headers)
    assert r.status_code == 200
    assert r.json()["agent_id"] in ("demo-reliable-agent", "demo-unreliable-agent")
