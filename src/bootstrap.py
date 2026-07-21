"""
Wires up a single shared Supervisor instance with a starting roster of
agents, used by both the API and the Streamlit dashboard. Always registers
the two demo workers (no dependencies needed). Best-effort registers the
real Phase 1 escalation agent if that repo is present as a sibling directory
and ANTHROPIC_API_KEY is set -- if not, the system still runs fully on the
demo workers.
"""
import os

from dotenv import load_dotenv

from .core import AgentProfile, AutonomyLevel, AuditLog, Supervisor
from .integrations import ReliableDemoWorker, UnreliableDemoWorker

# Bug fix: python-dotenv was a listed dependency but never actually invoked,
# so a correctly-filled .env file was silently never read by plain `python`
# invocations. This is the single entry point everything else goes through
# (API, Streamlit, and interactive/REPL use all call get_supervisor()), so
# loading here covers all of them. Safe to call even if .env doesn't exist.
load_dotenv()

_supervisor: Supervisor | None = None


def get_supervisor() -> Supervisor:
    global _supervisor
    if _supervisor is not None:
        return _supervisor

    audit = AuditLog(db_path=os.environ.get("AUDIT_DB_PATH", "agent_ops.db"))
    from .core.state_store import StateStore
    store = StateStore(db_path=os.environ.get("STATE_DB_PATH", "agent_ops_state.db"))
    supervisor = Supervisor(audit_log=audit, state_store=store)

    supervisor.register_agent(
        AgentProfile(
            agent_id="demo-reliable-agent",
            role="Demo: Reliable Worker",
            allowed_task_types=["demo_task"],
            autonomy_level=AutonomyLevel.L0_APPROVE_EVERY_ACTION,
            wip_limit=3,
        ),
        ReliableDemoWorker(),
    )
    supervisor.register_agent(
        AgentProfile(
            agent_id="demo-unreliable-agent",
            role="Demo: Unreliable Worker",
            allowed_task_types=["demo_task"],
            autonomy_level=AutonomyLevel.L2_REVIEW_AFTER,
            wip_limit=3,
        ),
        UnreliableDemoWorker(),
    )

    try:
        from .integrations.escalation_agent_http_worker import (
            EscalationAgentHTTPWorker, is_escalation_agent_reachable, _base_url,
        )
        if is_escalation_agent_reachable():
            supervisor.register_agent(
                AgentProfile(
                    agent_id="jira-escalation-agent",
                    role="Ticket Triage Analyst (Phase 1 escalation agent, called over HTTP)",
                    allowed_task_types=["ticket_triage"],
                    autonomy_level=AutonomyLevel.L1_APPROVE_HIGH_RISK,
                    wip_limit=2,
                ),
                EscalationAgentHTTPWorker(),
            )
        else:
            import logging
            logging.getLogger("agent_ops.bootstrap").info(
                "escalation-agent not reachable at %s -- skipping registration. "
                "Start it with: uvicorn src.api.main:app --port 8001 (from escalation-agent, its own venv). "
                "Demo agents still work without it.", _base_url(),
            )
    except ImportError:
        pass  # `requests` not installed -- fine, demo agents still work

    _supervisor = supervisor
    return supervisor
