"""
Trust-based task routing. Instead of the caller choosing which agent gets a
task, the supervisor can staff it -- the way a manager assigns work to
whoever on the team is best placed to take it.

Routing policy: among agents that (a) are scoped for the task type, (b) are
not paused, and (c) have WIP headroom, pick the one with the highest trust
score. Trust = rolling average quality, with a mild optimism prior for
agents that haven't completed enough tasks to have a track record yet (so
new agents get a chance to prove themselves rather than being starved of
work by incumbents -- the cold-start problem, handled the same way a manager
gives a new hire real work under closer review).
"""
from typing import Optional

from .models import AgentProfile, Task

NEW_AGENT_PRIOR = 0.6  # optimism prior for agents with no track record yet
MIN_TRACK_RECORD = 3   # completed tasks before rolling average fully replaces the prior


class NoEligibleAgentError(Exception):
    pass


def trust_score(agent: AgentProfile) -> float:
    """Blend the optimism prior with observed quality until the agent has a
    real track record, then use observed quality alone."""
    avg = agent.rolling_average_quality
    if avg is None:
        return NEW_AGENT_PRIOR
    n = min(agent.completed_task_count, MIN_TRACK_RECORD)
    weight = n / MIN_TRACK_RECORD
    return (weight * avg) + ((1 - weight) * NEW_AGENT_PRIOR)


def select_agent(agents: dict[str, AgentProfile], task: Task) -> Optional[str]:
    """Return the agent_id best placed to take this task, or raise if nobody can."""
    eligible = [
        a for a in agents.values()
        if task.task_type in a.allowed_task_types
        and not a.is_paused
        and a.current_wip < a.wip_limit
    ]
    if not eligible:
        raise NoEligibleAgentError(
            f"No agent is currently eligible for task type '{task.task_type}' "
            f"(scoped, unpaused, and under WIP limit)."
        )
    best = max(eligible, key=trust_score)
    return best.agent_id
