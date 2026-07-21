"""
A scripted, narrated walkthrough of the whole system in one run -- good for
a recorded demo. Uses only the demo workers, so it needs no API key and no
Phase 1 repo present.

Usage: python -m demo.run_demo
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core import AgentProfile, AutonomyLevel, AuditLog, Supervisor, Task, TaskRisk
from src.core.supervisor import AgentPausedError
from src.integrations.demo_worker import ReliableDemoWorker, UnreliableDemoWorker


def line(msg=""):
    print(msg)


def section(title):
    print()
    print(f"=== {title} ===")


def main():
    db_path = "demo_run.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    supervisor = Supervisor(audit_log=AuditLog(db_path=db_path))

    section("1. Registering two agents, both starting with minimal autonomy")
    supervisor.register_agent(
        AgentProfile("reliable-agent", "Demo: Reliable Worker", ["demo_task"],
                     AutonomyLevel.L0_APPROVE_EVERY_ACTION, wip_limit=3),
        ReliableDemoWorker(),
    )
    supervisor.register_agent(
        AgentProfile("unreliable-agent", "Demo: Unreliable Worker", ["demo_task"],
                     AutonomyLevel.L2_REVIEW_AFTER, wip_limit=3),
        UnreliableDemoWorker(failure_rate=0.8),
    )
    for a in supervisor.agents.values():
        line(f"  {a.agent_id}: autonomy={a.autonomy_level.name}")

    section("2. Reliable agent completes 5 tasks (each requiring approval at L0)")
    for i in range(5):
        task = Task("demo_task", f"Reliable task {i}", "Task completed successfully.")
        pending = supervisor.assign_task("reliable-agent", task)
        result = supervisor.approve_task(task.task_id)
        line(f"  Task {i}: approved -> {result.status.value}, quality={result.quality_score}")

    agent = supervisor.agents["reliable-agent"]
    line(f"\n  Result: autonomy promoted to {agent.autonomy_level.name} "
         f"(avg quality {agent.rolling_average_quality:.2f} over the review cycle)")

    section("3. Unreliable agent completes 5 tasks (runs freely at L2, reviewed after)")
    for i in range(5):
        task = Task("demo_task", f"Unreliable task {i}", "Task completed successfully.")
        result = supervisor.assign_task("unreliable-agent", task)
        flag = " <- ESCALATED for human review (low quality)" if result.status.value == "ESCALATED" else ""
        line(f"  Task {i}: {result.status.value}, quality={result.quality_score}{flag}")

    agent = supervisor.agents["unreliable-agent"]
    line(f"\n  Result: autonomy demoted to {agent.autonomy_level.name} "
         f"(avg quality {agent.rolling_average_quality:.2f} over the review cycle)")

    section("4. Kill switch: pausing the unreliable agent")
    supervisor.pause_agent("unreliable-agent")
    try:
        supervisor.assign_task("unreliable-agent", Task("demo_task", "One more task", "Done."))
    except AgentPausedError as e:
        line(f"  New task correctly refused: {e}")

    section("5. Full audit trail for the unreliable agent")
    for event in reversed(supervisor.audit.query(agent_id="unreliable-agent", limit=30)):
        line(f"  {event['timestamp']} | {event['event_type']} | {event['details']}")

    try:
        os.remove(db_path)
    except OSError as e:
        # Non-fatal: on some platforms a just-closed file can briefly still be
        # locked. The demo's actual output is already complete and correct at
        # this point -- cleanup failing here should never look like the demo
        # itself failed.
        line(f"\n(Note: could not remove temp file {db_path} -- {e}. Harmless; delete it manually if you like.)")


if __name__ == "__main__":
    main()
