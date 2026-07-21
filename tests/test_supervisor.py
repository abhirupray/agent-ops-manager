import os


import pytest

from src.core.state_store import StateStore
from src.core import AgentProfile, AutonomyLevel, AuditLog, Supervisor, Task, TaskRisk, TaskStatus
from src.core.supervisor import AgentPausedError, WipLimitExceededError, UnsupportedTaskTypeError
from src.integrations.demo_worker import ReliableDemoWorker, UnreliableDemoWorker


def make_supervisor(tmp_path) -> Supervisor:
    audit = AuditLog(db_path=str(tmp_path / "test_audit.db"))
    return Supervisor(audit_log=audit,
                      state_store=StateStore(db_path=str(tmp_path / "test_state.db")))


def make_task(risk=TaskRisk.MEDIUM) -> Task:
    return Task(
        task_type="demo_task", description="Do a thing",
        definition_of_done="The thing is done.", risk=risk,
    )


# -- WIP limiter ---------------------------------------------------------------

def test_wip_limit_blocks_excess_tasks(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.register_agent(
        AgentProfile("agent-1", "Tester", ["demo_task"], AutonomyLevel.L4_FULLY_AUTONOMOUS, wip_limit=1),
        UnreliableDemoWorker(failure_rate=0.0),  # always "succeeds" per the demo worker's own logic
    )
    # WIP is only occupied DURING execution; since our worker runs synchronously and
    # instantly, WIP returns to 0 immediately after. We test the limit is enforced by
    # checking it directly rather than racing a synchronous call.
    agent = supervisor.agents["agent-1"]
    agent.active_task_ids.append("fake-in-flight-task")
    with pytest.raises(WipLimitExceededError):
        supervisor.assign_task("agent-1", make_task())


# -- Autonomy-based approval routing --------------------------------------------

def test_l0_agent_requires_approval_for_every_task(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.register_agent(
        AgentProfile("agent-1", "Tester", ["demo_task"], AutonomyLevel.L0_APPROVE_EVERY_ACTION),
        ReliableDemoWorker(),
    )
    result = supervisor.assign_task("agent-1", make_task(risk=TaskRisk.LOW))
    assert result.status == TaskStatus.PENDING_APPROVAL


def test_l1_agent_only_requires_approval_for_high_risk(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.register_agent(
        AgentProfile("agent-1", "Tester", ["demo_task"], AutonomyLevel.L1_APPROVE_HIGH_RISK),
        ReliableDemoWorker(),
    )
    low_risk_result = supervisor.assign_task("agent-1", make_task(risk=TaskRisk.LOW))
    assert low_risk_result.status == TaskStatus.COMPLETED

    high_risk_result = supervisor.assign_task("agent-1", make_task(risk=TaskRisk.HIGH))
    assert high_risk_result.status == TaskStatus.PENDING_APPROVAL


def test_l4_agent_runs_immediately_regardless_of_risk(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.register_agent(
        AgentProfile("agent-1", "Tester", ["demo_task"], AutonomyLevel.L4_FULLY_AUTONOMOUS),
        ReliableDemoWorker(),
    )
    result = supervisor.assign_task("agent-1", make_task(risk=TaskRisk.HIGH))
    assert result.status == TaskStatus.COMPLETED


def test_approve_then_execute_flow(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.register_agent(
        AgentProfile("agent-1", "Tester", ["demo_task"], AutonomyLevel.L0_APPROVE_EVERY_ACTION),
        ReliableDemoWorker(),
    )
    task = make_task()
    pending = supervisor.assign_task("agent-1", task)
    assert pending.status == TaskStatus.PENDING_APPROVAL

    approved = supervisor.approve_task(task.task_id)
    assert approved.status == TaskStatus.COMPLETED
    assert approved.quality_score is not None


def test_reject_flow(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.register_agent(
        AgentProfile("agent-1", "Tester", ["demo_task"], AutonomyLevel.L0_APPROVE_EVERY_ACTION),
        ReliableDemoWorker(),
    )
    task = make_task()
    supervisor.assign_task("agent-1", task)
    rejected = supervisor.reject_task(task.task_id, reason="Not needed anymore")
    assert rejected.status == TaskStatus.REJECTED


# -- Kill switch ---------------------------------------------------------------

def test_paused_agent_rejects_new_tasks(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.register_agent(
        AgentProfile("agent-1", "Tester", ["demo_task"], AutonomyLevel.L4_FULLY_AUTONOMOUS),
        ReliableDemoWorker(),
    )
    supervisor.pause_agent("agent-1")
    with pytest.raises(AgentPausedError):
        supervisor.assign_task("agent-1", make_task())

    supervisor.resume_agent("agent-1")
    result = supervisor.assign_task("agent-1", make_task())  # works again after resume
    assert result.status == TaskStatus.COMPLETED


# -- Task scoping ---------------------------------------------------------------

def test_agent_rejects_out_of_scope_task_type(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.register_agent(
        AgentProfile("agent-1", "Tester", ["demo_task"], AutonomyLevel.L4_FULLY_AUTONOMOUS),
        ReliableDemoWorker(),
    )
    out_of_scope_task = Task(task_type="some_other_task", description="x", definition_of_done="x")
    with pytest.raises(UnsupportedTaskTypeError):
        supervisor.assign_task("agent-1", out_of_scope_task)
