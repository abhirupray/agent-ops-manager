import os


from src.core.state_store import StateStore
from src.core import AgentProfile, AutonomyLevel, AuditLog, Supervisor, TaskRisk
from src.core.policy import EVALUATION_WINDOW
from src.integrations.demo_worker import ReliableDemoWorker, UnreliableDemoWorker
from src.integrations.escalation_agent_http_worker import make_ticket_triage_task
from src.core.models import Task


def make_supervisor(tmp_path) -> Supervisor:
    audit = AuditLog(db_path=str(tmp_path / "test_audit.db"))
    return Supervisor(audit_log=audit,
                      state_store=StateStore(db_path=str(tmp_path / "test_state.db")))


def make_task() -> Task:
    return Task(task_type="demo_task", description="Do a thing", definition_of_done="The thing is done.")


def test_reliable_agent_gets_promoted_after_review_cycle(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.register_agent(
        AgentProfile("agent-1", "Tester", ["demo_task"], AutonomyLevel.L0_APPROVE_EVERY_ACTION),
        ReliableDemoWorker(),
    )
    agent = supervisor.agents["agent-1"]
    assert agent.autonomy_level == AutonomyLevel.L0_APPROVE_EVERY_ACTION

    for _ in range(EVALUATION_WINDOW):
        task = make_task()
        supervisor.assign_task("agent-1", task)  # L0 -> goes to pending approval
        supervisor.approve_task(task.task_id)     # human approves -> executes -> scores high

    assert agent.autonomy_level == AutonomyLevel.L1_APPROVE_HIGH_RISK
    assert agent.completed_task_count == EVALUATION_WINDOW


def test_unreliable_agent_gets_demoted_after_review_cycle(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.register_agent(
        AgentProfile("agent-1", "Tester", ["demo_task"], AutonomyLevel.L2_REVIEW_AFTER),
        UnreliableDemoWorker(failure_rate=1.0),  # always produces a failure-flavored output
    )
    agent = supervisor.agents["agent-1"]

    for _ in range(EVALUATION_WINDOW):
        supervisor.assign_task("agent-1", make_task())  # L2 -> runs immediately, no approval needed

    assert agent.autonomy_level == AutonomyLevel.L1_APPROVE_HIGH_RISK
    assert agent.rolling_average_quality < 0.45


def test_no_adjustment_before_review_cycle_completes(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.register_agent(
        AgentProfile("agent-1", "Tester", ["demo_task"], AutonomyLevel.L0_APPROVE_EVERY_ACTION),
        ReliableDemoWorker(),
    )
    agent = supervisor.agents["agent-1"]

    for _ in range(EVALUATION_WINDOW - 1):
        task = make_task()
        supervisor.assign_task("agent-1", task)
        supervisor.approve_task(task.task_id)

    assert agent.autonomy_level == AutonomyLevel.L0_APPROVE_EVERY_ACTION  # not yet, one short of the cycle


def test_audit_log_captures_full_lifecycle(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.register_agent(
        AgentProfile("agent-1", "Tester", ["demo_task"], AutonomyLevel.L4_FULLY_AUTONOMOUS),
        ReliableDemoWorker(),
    )
    supervisor.assign_task("agent-1", make_task())

    events = [e["event_type"] for e in supervisor.audit.query(agent_id="agent-1")]
    assert "AGENT_REGISTERED" in events
    assert "TASK_ASSIGNED" in events
    assert "TASK_EXECUTION_STARTED" in events
    assert "TASK_COMPLETED" in events


def test_make_ticket_triage_task_helper():
    task = make_ticket_triage_task("PROJ-104", risk=TaskRisk.HIGH)
    assert task.task_type == "ticket_triage"
    assert task.payload == {"ticket_key": "PROJ-104"}
    assert task.risk == TaskRisk.HIGH
