"""
Quality checking is pluggable (see DECISIONS.md for why). Every task has a
mandatory `definition_of_done`, which is what makes automatic scoring
possible without a human in the loop for every single task.

HeuristicQualityChecker is the default: fast, free, deterministic, and good
enough to drive promotion/demotion logic and tests without needing an API
key. LLMQualityChecker is the production-grade option -- it actually reads
the output against the definition of done using Claude as a judge.
"""
from typing import Protocol

from .models import Task


class QualityChecker(Protocol):
    def score(self, task: Task, output) -> tuple[float, str]:
        """Return (score in [0.0, 1.0], reasoning string)."""
        ...


class HeuristicQualityChecker:
    """A fast, free, deterministic scorer: checks that the output is non-empty,
    is reasonably substantial, and doesn't contain obvious failure markers.
    This is intentionally shallow -- it exists so the supervisor's promotion/
    demotion/escalation logic can be fully tested without an LLM call, not to
    be a good judge of actual task quality. Swap in LLMQualityChecker for that."""

    FAILURE_MARKERS = ("error", "exception", "failed", "traceback", "none")

    def score(self, task: Task, output) -> tuple[float, str]:
        text = str(output).strip()
        if not text:
            return 0.0, "Output was empty."

        lowered = text.lower()
        marker_hits = [m for m in self.FAILURE_MARKERS if m in lowered]
        if marker_hits:
            return 0.2, f"Output contains failure markers: {marker_hits}."

        if len(text) < 10:
            return 0.4, "Output is suspiciously short for the stated task."

        return 0.9, "Output is non-empty, substantial, and contains no obvious failure markers."


class LLMQualityChecker:
    """Uses Claude to judge whether the output actually satisfies the task's
    definition_of_done -- a real quality signal, not just a shape check.
    Requires ANTHROPIC_API_KEY."""

    def __init__(self, model: str = "claude-sonnet-4-6"):
        self.model = model

    def score(self, task: Task, output) -> tuple[float, str]:
        from langchain_anthropic import ChatAnthropic
        from pydantic import BaseModel, Field
        import os

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is required for LLMQualityChecker.")

        class Verdict(BaseModel):
            score: float = Field(ge=0.0, le=1.0, description="Quality score from 0.0 (fails the definition of done) to 1.0 (fully satisfies it)")
            reasoning: str = Field(description="Brief explanation of the score")

        judge = ChatAnthropic(model=self.model, temperature=0).with_structured_output(Verdict)
        verdict = judge.invoke(
            f"Task: {task.description}\n"
            f"Definition of done: {task.definition_of_done}\n\n"
            f"Output produced:\n{output}\n\n"
            f"Score how well the output satisfies the definition of done."
        )
        return verdict.score, verdict.reasoning
