"""
Human feedback loop -- the piece that makes the system genuinely improve
through interaction, without any model retraining.

When a human reviews a completed/escalated task and disagrees with the
automated quality score, their verdict is recorded, overrides the automated
score in the agent's rolling history, and is logged to the audit trail.
Over time this means agent trust (and therefore autonomy and routing) is
shaped by human judgment, not just the automated checker -- exactly like a
manager's assessment of a report being informed by real feedback, not just
metrics.

This is system-level learning: the models stay stateless, but the
supervision layer's decisions get better calibrated with every correction.
"""
from dataclasses import dataclass
from typing import Optional

from .audit import AuditLog
from .models import AgentProfile


@dataclass
class HumanFeedback:
    task_id: str
    corrected_score: float   # 0.0-1.0, the human's verdict on quality
    note: str = ""


def apply_feedback(
    agent: AgentProfile,
    feedback: HumanFeedback,
    original_score: Optional[float],
    audit: AuditLog,
) -> None:
    """Fold the human verdict into the agent's rolling trust history and log it.

    Note on the replacement strategy: we replace the MOST RECENT occurrence of
    the original score (searching from the end), not the first match -- scores
    are floats and two different tasks can share the same value, so matching
    from the front could silently rewrite the wrong task's contribution. The
    fully correct fix is storing (task_id, score) pairs in the rolling window;
    that's noted in DECISIONS.md as the next step if the window ever needs to
    be queryable per-task."""
    if original_score is not None and original_score in agent.rolling_quality_scores:
        # Find the LAST occurrence (most recent task with this score).
        idx = len(agent.rolling_quality_scores) - 1 - agent.rolling_quality_scores[::-1].index(original_score)
        agent.rolling_quality_scores[idx] = feedback.corrected_score
    else:
        # Original score already rotated out of the window -- append the human
        # verdict so it still influences the trust calculation going forward.
        agent.rolling_quality_scores.append(feedback.corrected_score)

    audit.log(
        "HUMAN_FEEDBACK_APPLIED",
        agent_id=agent.agent_id,
        task_id=feedback.task_id,
        details={
            "original_automated_score": original_score,
            "human_corrected_score": feedback.corrected_score,
            "note": feedback.note,
        },
    )
