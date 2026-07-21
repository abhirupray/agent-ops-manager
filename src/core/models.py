"""
Core domain models for the Agent Ops Manager.

The central idea (see DECISIONS.md and README): agents are supervised the way
an engineering manager supervises a team -- each has a defined role and scope,
an autonomy level that's earned through demonstrated performance rather than
assumed, a WIP limit, and an escalation path for anything outside its
competence or confidence.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4


class AutonomyLevel(int, Enum):
    """The delegation ladder. Modeled directly on how a manager delegates to a
    new hire vs. a trusted senior engineer -- autonomy is earned, not assumed."""
    L0_APPROVE_EVERY_ACTION = 0     # Every task requires human sign-off before execution
    L1_APPROVE_HIGH_RISK = 1        # Low/medium-risk tasks auto-run; high-risk needs approval first
    L2_REVIEW_AFTER = 2             # Agent acts autonomously; every result is reviewed after the fact
    L3_SAMPLED_AUDIT = 3            # Agent acts autonomously within its role; a sample of results is audited
    L4_FULLY_AUTONOMOUS = 4         # Agent acts autonomously; audit only triggers on anomaly/escalation


class TaskRisk(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class TaskStatus(str, Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    ESCALATED = "ESCALATED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Task:
    """A unit of work assigned to an agent. `definition_of_done` is mandatory
    by design -- no task enters the system without a stated success criterion,
    which is what makes automatic quality scoring possible at all."""
    task_type: str
    description: str
    definition_of_done: str
    payload: dict = field(default_factory=dict)
    risk: TaskRisk = TaskRisk.MEDIUM
    task_id: str = field(default_factory=lambda: str(uuid4())[:8])
    created_at: str = field(default_factory=_now)


@dataclass
class TaskResult:
    task_id: str
    agent_id: str
    status: TaskStatus
    output: Any = None
    quality_score: Optional[float] = None       # 0.0-1.0, from the quality checker
    quality_reasoning: Optional[str] = None
    duration_seconds: Optional[float] = None
    completed_at: str = field(default_factory=_now)


@dataclass
class AgentProfile:
    """A registered agent. Mirrors what you'd put in a job description: a
    defined role, a defined scope of allowed task types, and a WIP limit --
    plus the state that changes over time as the agent proves itself."""
    agent_id: str
    role: str
    allowed_task_types: list[str]
    autonomy_level: AutonomyLevel = AutonomyLevel.L0_APPROVE_EVERY_ACTION
    wip_limit: int = 3
    active_task_ids: list[str] = field(default_factory=list)
    is_paused: bool = False
    rolling_quality_scores: list[float] = field(default_factory=list)  # most recent N task scores
    completed_task_count: int = 0

    @property
    def current_wip(self) -> int:
        return len(self.active_task_ids)

    @property
    def rolling_average_quality(self) -> Optional[float]:
        if not self.rolling_quality_scores:
            return None
        return sum(self.rolling_quality_scores) / len(self.rolling_quality_scores)
