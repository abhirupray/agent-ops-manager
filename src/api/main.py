"""
FastAPI service for the Agent Ops Manager platform.

Security model (see src/api/auth.py for the full rationale):
  - ADMIN: agent lifecycle (pause/resume), task assignment/routing
  - REVIEWER or ADMIN: read endpoints, escalation approve/reject, feedback
  - /health: unauthenticated (load balancers need it)

All responses use explicit Pydantic response models -- internal dataclasses
never leak through the API boundary (`__dict__` serialization was an audit
finding: it exposes internals and silently changes shape when internals do).
"""
import logging
import os

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..bootstrap import get_supervisor
from ..core import Task, TaskRisk
from ..core.router import NoEligibleAgentError
from ..core.supervisor import AgentPausedError, UnknownAgentError, UnsupportedTaskTypeError, WipLimitExceededError
from .auth import Role, require_admin, require_reviewer_or_admin

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("agent_ops.api")

app = FastAPI(
    title="Agent Ops Manager",
    description="Supervises AI agents with earned autonomy, WIP limits, human escalation, "
                "and a full audit trail.",
    version="2.0.0",
)

# CORS: default deny-all-origins unless explicitly configured (audit finding:
# the previous wildcard default was unacceptable for a governance product).
_allowed_origins = [o for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o]
app.add_middleware(CORSMiddleware, allow_origins=_allowed_origins,
                    allow_methods=["*"], allow_headers=["*"])


# -- Response / request models ---------------------------------------------------

class AgentSummary(BaseModel):
    agent_id: str
    role: str
    allowed_task_types: list[str]
    autonomy_level: str
    autonomy_level_value: int
    wip_limit: int
    current_wip: int
    is_paused: bool
    completed_task_count: int
    rolling_average_quality: float | None


class TaskResultResponse(BaseModel):
    task_id: str
    agent_id: str
    status: str
    output: str | None = None
    quality_score: float | None = None
    quality_reasoning: str | None = None
    duration_seconds: float | None = None


class AssignTaskRequest(BaseModel):
    agent_id: str | None = Field(default=None,
        description="Target agent. Omit to let the supervisor route by trust.")
    task_type: str
    description: str
    definition_of_done: str
    payload: dict = {}
    risk: TaskRisk = TaskRisk.MEDIUM


class EscalationSummary(BaseModel):
    task_id: str
    agent_id: str
    description: str
    risk: str
    task_type: str


class FeedbackRequest(BaseModel):
    corrected_score: float = Field(ge=0.0, le=1.0)
    note: str = ""


class RejectRequest(BaseModel):
    reason: str = ""


class PauseResponse(BaseModel):
    agent_id: str
    is_paused: bool


def _agent_summary(agent) -> AgentSummary:
    return AgentSummary(
        agent_id=agent.agent_id, role=agent.role,
        allowed_task_types=agent.allowed_task_types,
        autonomy_level=agent.autonomy_level.name,
        autonomy_level_value=int(agent.autonomy_level),
        wip_limit=agent.wip_limit, current_wip=agent.current_wip,
        is_paused=agent.is_paused, completed_task_count=agent.completed_task_count,
        rolling_average_quality=agent.rolling_average_quality,
    )


def _result_response(result) -> TaskResultResponse:
    return TaskResultResponse(
        task_id=result.task_id, agent_id=result.agent_id, status=result.status.value,
        output=str(result.output) if result.output is not None else None,
        quality_score=result.quality_score, quality_reasoning=result.quality_reasoning,
        duration_seconds=result.duration_seconds,
    )


# -- Routes -----------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/agents", response_model=list[AgentSummary])
def list_agents(role: Role = Depends(require_reviewer_or_admin)):
    return [_agent_summary(a) for a in get_supervisor().agents.values()]


@app.get("/agents/{agent_id}/audit")
def agent_audit(agent_id: str, role: Role = Depends(require_reviewer_or_admin)):
    supervisor = get_supervisor()
    if agent_id not in supervisor.agents:
        raise HTTPException(status_code=404, detail=f"No agent with id {agent_id}")
    return supervisor.audit.query(agent_id=agent_id)


@app.post("/tasks/assign", response_model=TaskResultResponse)
def assign_task(req: AssignTaskRequest, role: Role = Depends(require_admin)):
    supervisor = get_supervisor()
    task = Task(task_type=req.task_type, description=req.description,
                definition_of_done=req.definition_of_done, payload=req.payload, risk=req.risk)
    try:
        if req.agent_id:
            logger.info("task_assign agent=%s type=%s risk=%s", req.agent_id, req.task_type, req.risk.value)
            result = supervisor.assign_task(req.agent_id, task)
        else:
            logger.info("task_route type=%s risk=%s", req.task_type, req.risk.value)
            result = supervisor.route_task(task)
    except (UnknownAgentError, UnsupportedTaskTypeError, NoEligibleAgentError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except AgentPausedError as e:
        raise HTTPException(status_code=423, detail=str(e))
    except WipLimitExceededError as e:
        raise HTTPException(status_code=429, detail=str(e))
    return _result_response(result)


@app.get("/escalations", response_model=list[EscalationSummary])
def list_escalations(role: Role = Depends(require_reviewer_or_admin)):
    supervisor = get_supervisor()
    return [
        EscalationSummary(task_id=t.task_id,
                           agent_id=supervisor._pending_agent_for_task[t.task_id],
                           description=t.description, risk=t.risk.value, task_type=t.task_type)
        for t in supervisor.pending_approvals.values()
    ]


@app.post("/escalations/{task_id}/approve", response_model=TaskResultResponse)
def approve_escalation(task_id: str, role: Role = Depends(require_reviewer_or_admin)):
    try:
        result = get_supervisor().approve_task(task_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    logger.info("escalation_approved task=%s by_role=%s", task_id, role.value)
    return _result_response(result)


@app.post("/escalations/{task_id}/reject", response_model=TaskResultResponse)
def reject_escalation(task_id: str, req: RejectRequest = RejectRequest(),
                       role: Role = Depends(require_reviewer_or_admin)):
    try:
        result = get_supervisor().reject_task(task_id, reason=req.reason)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    logger.info("escalation_rejected task=%s by_role=%s", task_id, role.value)
    return _result_response(result)


@app.post("/tasks/{task_id}/feedback")
def submit_feedback(task_id: str, req: FeedbackRequest,
                     role: Role = Depends(require_reviewer_or_admin)):
    try:
        get_supervisor().record_human_feedback(task_id, req.corrected_score, req.note)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    logger.info("feedback task=%s score=%s by_role=%s", task_id, req.corrected_score, role.value)
    return {"task_id": task_id, "corrected_score": req.corrected_score}


@app.post("/agents/{agent_id}/pause", response_model=PauseResponse)
def pause_agent(agent_id: str, role: Role = Depends(require_admin)):
    try:
        get_supervisor().pause_agent(agent_id)
    except UnknownAgentError as e:
        raise HTTPException(status_code=404, detail=str(e))
    logger.warning("kill_switch_engaged agent=%s", agent_id)
    return PauseResponse(agent_id=agent_id, is_paused=True)


@app.post("/agents/{agent_id}/resume", response_model=PauseResponse)
def resume_agent(agent_id: str, role: Role = Depends(require_admin)):
    try:
        get_supervisor().resume_agent(agent_id)
    except UnknownAgentError as e:
        raise HTTPException(status_code=404, detail=str(e))
    logger.info("agent_resumed agent=%s", agent_id)
    return PauseResponse(agent_id=agent_id, is_paused=False)
