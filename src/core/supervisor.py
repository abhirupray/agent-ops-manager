"""
The Supervisor. This is the whole system's center of gravity: it's the thing
that decides whether a task can be assigned, whether it needs human
approval first, executes it, scores the result, updates the agent's
standing, and logs every step. Every other module in this repo exists to
support what happens here.
"""
import time
from typing import Optional

from .audit import AuditLog
from .models import AgentProfile, AutonomyLevel, Task, TaskResult, TaskRisk, TaskStatus
from .policy import EVALUATION_WINDOW, maybe_adjust_autonomy
from .quality import HeuristicQualityChecker, QualityChecker
from .worker import AgentWorker

POST_HOC_ESCALATION_THRESHOLD = 0.5


class AgentPausedError(Exception):
    pass


class WipLimitExceededError(Exception):
    pass


class UnknownAgentError(Exception):
    pass


class UnsupportedTaskTypeError(Exception):
    pass


class Supervisor:
    def __init__(self, audit_log: Optional[AuditLog] = None,
                 quality_checker: Optional[QualityChecker] = None,
                 state_store=None):
        from .state_store import StateStore
        self.agents: dict[str, AgentProfile] = {}
        self.workers: dict[str, AgentWorker] = {}
        self.audit = audit_log or AuditLog()
        self.quality_checker = quality_checker or HeuristicQualityChecker()
        self.store = state_store or StateStore()
        self.pending_approvals: dict[str, Task] = {}
        self._pending_agent_for_task: dict[str, str] = {}
        self.results: dict[str, TaskResult] = {}
        # Hydrate pending approvals from persistence so approvals survive
        # process restarts and are shared across processes (API + dashboard).
        for task, agent_id in self.store.load_all_pending():
            self.pending_approvals[task.task_id] = task
            self._pending_agent_for_task[task.task_id] = agent_id

    # -- Registration & lifecycle -------------------------------------------------

    def register_agent(self, profile: AgentProfile, worker: AgentWorker) -> None:
        """Register an agent's worker (code) and profile (state). If a persisted
        profile exists for this agent_id, the persisted STATE (autonomy, trust
        history, paused flag) wins over the passed-in defaults -- earned trust
        must survive restarts; code-level defaults are only for first boot."""
        persisted = self.store.load_agent(profile.agent_id)
        if persisted is not None:
            profile.autonomy_level = persisted.autonomy_level
            profile.is_paused = persisted.is_paused
            profile.rolling_quality_scores = persisted.rolling_quality_scores
            profile.completed_task_count = persisted.completed_task_count
        self.agents[profile.agent_id] = profile
        self.workers[profile.agent_id] = worker
        self.store.save_agent(profile)
        self.audit.log(
            "AGENT_REGISTERED", agent_id=profile.agent_id,
            details={"role": profile.role, "autonomy_level": profile.autonomy_level.name,
                     "allowed_task_types": profile.allowed_task_types, "wip_limit": profile.wip_limit,
                     "hydrated_from_persistence": persisted is not None},
        )

    def pause_agent(self, agent_id: str) -> None:
        """The kill switch. Prevents any NEW task assignment. Does not interrupt
        a task already mid-execution -- see DECISIONS.md for why, and what a
        production version would add."""
        agent = self._get_agent(agent_id)
        agent.is_paused = True
        self.store.save_agent(agent)
        self.audit.log("AGENT_PAUSED", agent_id=agent_id)

    def resume_agent(self, agent_id: str) -> None:
        agent = self._get_agent(agent_id)
        agent.is_paused = False
        self.store.save_agent(agent)
        self.audit.log("AGENT_RESUMED", agent_id=agent_id)

    def _get_agent(self, agent_id: str) -> AgentProfile:
        if agent_id not in self.agents:
            raise UnknownAgentError(f"No agent registered with id '{agent_id}'.")
        return self.agents[agent_id]

    # -- Trust-based routing ---------------------------------------------------

    def route_task(self, task: Task) -> TaskResult:
        """Let the supervisor staff the task: picks the most trusted eligible
        agent for this task type (see src/core/router.py) and assigns it."""
        from .router import select_agent
        agent_id = select_agent(self.agents, task)
        self.audit.log("TASK_ROUTED", agent_id=agent_id, task_id=task.task_id,
                        details={"task_type": task.task_type, "routing": "trust_based"})
        return self.assign_task(agent_id, task)

    # -- Human feedback loop ---------------------------------------------------

    def record_human_feedback(self, task_id: str, corrected_score: float, note: str = "") -> None:
        """A human reviewer's verdict on a completed task. Overrides the
        automated quality score in the agent's rolling trust history -- this is
        how the system's judgment improves through interaction (see
        src/core/feedback.py)."""
        from .feedback import HumanFeedback, apply_feedback
        result = self.results.get(task_id) or self.store.load_result(task_id)
        if result is None:
            raise KeyError(f"No recorded result for task '{task_id}'.")
        agent = self._get_agent(result.agent_id)
        apply_feedback(
            agent,
            HumanFeedback(task_id=task_id, corrected_score=corrected_score, note=note),
            original_score=result.quality_score,
            audit=self.audit,
        )
        result.quality_score = corrected_score
        self.results[task_id] = result
        self.store.save_result(result)
        self.store.save_agent(agent)

    # -- Task assignment ------------------------------------------------------

    def _requires_preapproval(self, agent: AgentProfile, task: Task) -> bool:
        if agent.autonomy_level == AutonomyLevel.L0_APPROVE_EVERY_ACTION:
            return True
        if agent.autonomy_level == AutonomyLevel.L1_APPROVE_HIGH_RISK and task.risk == TaskRisk.HIGH:
            return True
        return False

    def assign_task(self, agent_id: str, task: Task) -> TaskResult:
        agent = self._get_agent(agent_id)

        if agent.is_paused:
            self.audit.log("TASK_REJECTED_PAUSED", agent_id=agent_id, task_id=task.task_id)
            raise AgentPausedError(f"Agent '{agent_id}' is paused (kill switch active); refusing new task assignment.")

        if task.task_type not in agent.allowed_task_types:
            raise UnsupportedTaskTypeError(
                f"Agent '{agent_id}' is not scoped for task type '{task.task_type}'. "
                f"Allowed: {agent.allowed_task_types}"
            )

        if agent.current_wip >= agent.wip_limit:
            self.audit.log("TASK_REJECTED_WIP_LIMIT", agent_id=agent_id, task_id=task.task_id,
                            details={"wip_limit": agent.wip_limit, "current_wip": agent.current_wip})
            raise WipLimitExceededError(f"Agent '{agent_id}' is at its WIP limit ({agent.wip_limit}).")

        self.audit.log("TASK_ASSIGNED", agent_id=agent_id, task_id=task.task_id,
                        details={"task_type": task.task_type, "risk": task.risk.value})

        if self._requires_preapproval(agent, task):
            self.pending_approvals[task.task_id] = task
            self._pending_agent_for_task[task.task_id] = agent_id
            self.store.save_pending(task, agent_id)
            self.audit.log("TASK_PENDING_APPROVAL", agent_id=agent_id, task_id=task.task_id,
                            details={"autonomy_level": agent.autonomy_level.name, "risk": task.risk.value})
            result = TaskResult(task_id=task.task_id, agent_id=agent_id, status=TaskStatus.PENDING_APPROVAL)
            self.results[task.task_id] = result
            self.store.save_result(result)
            return result

        return self._execute(agent, task)

    def approve_task(self, task_id: str) -> TaskResult:
        if task_id not in self.pending_approvals:
            raise KeyError(f"No pending task with id '{task_id}'.")
        task = self.pending_approvals.pop(task_id)
        agent_id = self._pending_agent_for_task.pop(task_id)
        self.store.delete_pending(task_id)
        agent = self._get_agent(agent_id)
        self.audit.log("TASK_APPROVED_BY_HUMAN", agent_id=agent_id, task_id=task_id)
        return self._execute(agent, task)

    def reject_task(self, task_id: str, reason: str = "") -> TaskResult:
        if task_id not in self.pending_approvals:
            raise KeyError(f"No pending task with id '{task_id}'.")
        task = self.pending_approvals.pop(task_id)
        agent_id = self._pending_agent_for_task.pop(task_id)
        self.store.delete_pending(task_id)
        self.audit.log("TASK_REJECTED_BY_HUMAN", agent_id=agent_id, task_id=task_id, details={"reason": reason})
        result = TaskResult(task_id=task_id, agent_id=agent_id, status=TaskStatus.REJECTED)
        self.results[task_id] = result
        self.store.save_result(result)
        return result

    # -- Execution --------------------------------------------------------------

    def _execute(self, agent: AgentProfile, task: Task) -> TaskResult:
        agent.active_task_ids.append(task.task_id)
        self.audit.log("TASK_EXECUTION_STARTED", agent_id=agent.agent_id, task_id=task.task_id)
        start = time.monotonic()
        worker = self.workers[agent.agent_id]

        try:
            output = worker.run(task)
        except Exception as e:
            agent.active_task_ids.remove(task.task_id)
            self.store.save_agent(agent)
            self.audit.log("TASK_FAILED", agent_id=agent.agent_id, task_id=task.task_id, details={"error": str(e)})
            result = TaskResult(task_id=task.task_id, agent_id=agent.agent_id, status=TaskStatus.FAILED, output=str(e))
            self.results[task.task_id] = result
            self.store.save_result(result)
            return result

        duration = time.monotonic() - start
        agent.active_task_ids.remove(task.task_id)

        score, reasoning = self.quality_checker.score(task, output)
        agent.rolling_quality_scores.append(score)
        if len(agent.rolling_quality_scores) > EVALUATION_WINDOW:
            agent.rolling_quality_scores = agent.rolling_quality_scores[-EVALUATION_WINDOW:]
        agent.completed_task_count += 1

        status = TaskStatus.COMPLETED
        if score < POST_HOC_ESCALATION_THRESHOLD and agent.autonomy_level >= AutonomyLevel.L2_REVIEW_AFTER:
            # Even at higher autonomy, a badly-scored result still gets surfaced to a human --
            # autonomy changes who reviews BEFORE execution, not whether bad results get caught.
            status = TaskStatus.ESCALATED
            self.audit.log("TASK_ESCALATED_LOW_QUALITY", agent_id=agent.agent_id, task_id=task.task_id,
                            details={"score": score, "reasoning": reasoning})

        self.audit.log("TASK_COMPLETED", agent_id=agent.agent_id, task_id=task.task_id,
                        details={"score": score, "reasoning": reasoning, "duration_seconds": duration, "status": status.value})

        maybe_adjust_autonomy(agent, self.audit)
        self.store.save_agent(agent)

        result = TaskResult(
            task_id=task.task_id, agent_id=agent.agent_id, status=status, output=output,
            quality_score=score, quality_reasoning=reasoning, duration_seconds=duration,
        )
        self.results[task.task_id] = result
        self.store.save_result(result)
        return result
