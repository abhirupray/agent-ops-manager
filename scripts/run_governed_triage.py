"""
Runs the REAL escalation agent, fully governed by the Agent Ops Manager
supervisor, across every open ticket -- pulled dynamically over HTTP from
escalation-agent's own running API. No ticket keys are hardcoded here: the
list of tickets to triage comes from GET /tickets on the escalation-agent
service, exactly like escalation-agent's own `python -m src.cli` does
internally against its local data source.

This is the actual integration demo: every ticket goes through
supervisor.assign_task(), so WIP limits, the autonomy ladder, human-approval
gating, quality scoring, and the audit trail all apply to real agent
reasoning, not the mock demo workers -- and the two services talk to each
other purely over the network, each running independently in its own venv.

IMPORTANT: escalation-agent's API must be running first:
    cd escalation-agent && uvicorn src.api.main:app --port 8001

Usage:
    python scripts/run_governed_triage.py                 # holds risky tickets for approval
    python scripts/run_governed_triage.py --auto-approve   # hands-off demo run
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.bootstrap import get_supervisor
from src.core import TaskRisk
from src.integrations.escalation_agent_http_worker import get_open_ticket_keys, make_ticket_triage_task

# Maps escalation-agent's ticket priority field to the supervisor's risk
# levels, which is what determines whether a task needs human approval
# before running (see AutonomyLevel in src/core/models.py).
PRIORITY_TO_RISK = {
    "Critical": TaskRisk.HIGH,
    "High": TaskRisk.HIGH,
    "Medium": TaskRisk.MEDIUM,
    "Low": TaskRisk.LOW,
}


def main():
    parser = argparse.ArgumentParser(description="Run governed triage across all open tickets.")
    parser.add_argument(
        "--auto-approve", action="store_true",
        help="Automatically approve any task the supervisor holds for human review. "
             "Useful for a hands-off demo; in a real workflow a human reviews these "
             "via the dashboard or API instead.",
    )
    args = parser.parse_args()

    supervisor = get_supervisor()
    if "jira-escalation-agent" not in supervisor.agents:
        print("jira-escalation-agent is not registered. Check that:")
        print("  1. escalation-agent's API is running: uvicorn src.api.main:app --port 8001")
        print("     (in escalation-agent's OWN venv, in a separate terminal)")
        print("  2. ESCALATION_AGENT_URL in agent-ops-manager's .env points at it (default http://localhost:8001)")
        print("  3. ANTHROPIC_API_KEY is set in escalation-agent's .env (it does the LLM calls, not this repo)")
        sys.exit(1)

    ticket_keys = get_open_ticket_keys()
    print(f"Found {len(ticket_keys)} open ticket(s) via GET /tickets on escalation-agent's live API "
          f"(nothing hardcoded here -- this list comes over the network).\n")

    for ticket_key, priority in ticket_keys:
        risk = PRIORITY_TO_RISK.get(priority, TaskRisk.MEDIUM)
        task = make_ticket_triage_task(ticket_key, risk=risk)
        print(f"Assigning {ticket_key} ({priority} priority -> {risk.value} risk to the supervisor)...")
        result = supervisor.assign_task("jira-escalation-agent", task)

        if result.status.value == "PENDING_APPROVAL":
            if args.auto_approve:
                print("  Held for approval -- auto-approving (--auto-approve was passed)...")
                result = supervisor.approve_task(task.task_id)
            else:
                print(f"  Held for human approval (risk={risk.value}, autonomy requires sign-off). "
                      f"Approve via the dashboard, the API, or re-run with --auto-approve.")
                print()
                continue

        print(f"  -> {result.status.value} (quality score: {result.quality_score})")
        if result.output:
            first_line = str(result.output).splitlines()[0]
            print(f"     {first_line}")
        print()

    agent = supervisor.agents["jira-escalation-agent"]
    print("--- jira-escalation-agent status after this run ---")
    print(f"Autonomy level: {agent.autonomy_level.name}")
    print(f"Completed tasks: {agent.completed_task_count}")
    print(f"Rolling average quality: {agent.rolling_average_quality}")
    print(f"\nFull audit trail: supervisor.audit.query(agent_id='jira-escalation-agent') "
          f"or the Audit Trail tab in the dashboard.")


if __name__ == "__main__":
    main()
