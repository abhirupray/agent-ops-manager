import os


import pytest

from src.core.state_store import StateStore
from src.core import AgentProfile, AutonomyLevel, AuditLog, Supervisor, Task
from src.core.router import NoEligibleAgentError, select_agent, trust_score, NEW_AGENT_PRIOR
from src.integrations.demo_worker import ReliableDemoWorker, UnreliableDemoWorker


def make_supervisor(tmp_path) -> Supervisor:
    return Supervisor(audit_log=AuditLog(db_path=str(tmp_path / "test_audit.db")),
                      state_store=StateStore(db_path=str(tmp_path / "test_state.db")))


def make_task() -> Task:
    return Task(task_type="demo_task", description="Do a thing", definition_of_done="The thing is done.")


# -- Trust-based routing ---------------------------------------------------------

def test_router_prefers_agent_with_higher_track_record(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.register_agent(
        AgentProfile("good-agent", "Good", ["demo_task"], AutonomyLevel.L4_FULLY_AUTONOMOUS),
        ReliableDemoWorker(),
    )
    supervisor.register_agent(
        AgentProfile("bad-agent", "Bad", ["demo_task"], AutonomyLevel.L4_FULLY_AUTONOMOUS),
        UnreliableDemoWorker(failure_rate=1.0),
    )
    # Give both a track record
    for _ in range(3):
        supervisor.assign_task("good-agent", make_task())
        supervisor.assign_task("bad-agent", make_task())

    chosen = select_agent(supervisor.agents, make_task())
    assert chosen == "good-agent"


def test_router_gives_new_agent_optimism_prior():
    agent = AgentProfile("newbie", "New", ["demo_task"])
    assert trust_score(agent) == NEW_AGENT_PRIOR


def test_router_skips_paused_and_out_of_scope_agents(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.register_agent(
        AgentProfile("scoped-agent", "Scoped", ["other_task"], AutonomyLevel.L4_FULLY_AUTONOMOUS),
        ReliableDemoWorker(),
    )
    supervisor.register_agent(
        AgentProfile("paused-agent", "Paused", ["demo_task"], AutonomyLevel.L4_FULLY_AUTONOMOUS),
        ReliableDemoWorker(),
    )
    supervisor.pause_agent("paused-agent")
    with pytest.raises(NoEligibleAgentError):
        select_agent(supervisor.agents, make_task())


def test_route_task_end_to_end(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.register_agent(
        AgentProfile("only-agent", "Solo", ["demo_task"], AutonomyLevel.L4_FULLY_AUTONOMOUS),
        ReliableDemoWorker(),
    )
    task = make_task()
    result = supervisor.route_task(task)
    assert result.agent_id == "only-agent"
    events = [e["event_type"] for e in supervisor.audit.query(task_id=task.task_id)]
    assert "TASK_ROUTED" in events


# -- Human feedback loop ---------------------------------------------------------

def test_human_feedback_overrides_automated_score(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.register_agent(
        AgentProfile("agent-1", "Tester", ["demo_task"], AutonomyLevel.L4_FULLY_AUTONOMOUS),
        ReliableDemoWorker(),
    )
    task = make_task()
    result = supervisor.assign_task("agent-1", task)
    assert result.quality_score == 0.9  # heuristic score

    # Human disagrees: the output looked fine but was actually wrong
    supervisor.record_human_feedback(task.task_id, corrected_score=0.1, note="Output was confidently wrong.")

    agent = supervisor.agents["agent-1"]
    assert 0.1 in agent.rolling_quality_scores
    assert 0.9 not in agent.rolling_quality_scores
    assert supervisor.results[task.task_id].quality_score == 0.1

    events = supervisor.audit.query(task_id=task.task_id)
    feedback_events = [e for e in events if e["event_type"] == "HUMAN_FEEDBACK_APPLIED"]
    assert len(feedback_events) == 1
    assert feedback_events[0]["details"]["human_corrected_score"] == 0.1


def test_feedback_on_unknown_task_raises(tmp_path):
    supervisor = make_supervisor(tmp_path)
    with pytest.raises(KeyError):
        supervisor.record_human_feedback("nonexistent-task", corrected_score=0.5)
