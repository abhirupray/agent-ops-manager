"""
Promotion/demotion policy. Deliberately evaluated on a periodic cycle (every
EVALUATION_WINDOW completed tasks) rather than after every single task --
this mirrors a real performance review cadence and avoids an agent
oscillating up and down in autonomy after every individual result.
"""
from typing import Optional

from .audit import AuditLog
from .models import AgentProfile, AutonomyLevel

EVALUATION_WINDOW = 5
PROMOTE_THRESHOLD = 0.85
DEMOTE_THRESHOLD = 0.45


def maybe_adjust_autonomy(agent: AgentProfile, audit: AuditLog) -> Optional[str]:
    """Call after every completed task. Only actually adjusts autonomy on
    review-cycle boundaries (every EVALUATION_WINDOW completed tasks). Returns
    "PROMOTED", "DEMOTED", or None."""
    if agent.completed_task_count == 0 or agent.completed_task_count % EVALUATION_WINDOW != 0:
        return None
    avg = agent.rolling_average_quality
    if avg is None:
        return None

    old_level = agent.autonomy_level
    if avg >= PROMOTE_THRESHOLD and agent.autonomy_level < AutonomyLevel.L4_FULLY_AUTONOMOUS:
        agent.autonomy_level = AutonomyLevel(agent.autonomy_level + 1)
    elif avg < DEMOTE_THRESHOLD and agent.autonomy_level > AutonomyLevel.L0_APPROVE_EVERY_ACTION:
        agent.autonomy_level = AutonomyLevel(agent.autonomy_level - 1)

    if agent.autonomy_level == old_level:
        return None

    event = "PROMOTED" if agent.autonomy_level > old_level else "DEMOTED"
    audit.log(
        event,
        agent_id=agent.agent_id,
        details={
            "from": old_level.name,
            "to": agent.autonomy_level.name,
            "rolling_avg_quality": round(avg, 3),
            "at_completed_task_count": agent.completed_task_count,
        },
    )
    return event
