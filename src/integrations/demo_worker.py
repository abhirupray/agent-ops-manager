"""
Demo workers. These simulate agents of varying reliability so the
supervisor's promotion/demotion/escalation/kill-switch logic can be fully
exercised and demoed without needing Phase 1 (the escalation agent) or an
ANTHROPIC_API_KEY. See tests/test_supervisor.py and demo/run_demo.py.
"""
import random

from ..core.models import Task
from ..core.worker import AgentWorker


class ReliableDemoWorker(AgentWorker):
    """Simulates a consistently competent agent -- for demonstrating promotion."""

    def run(self, task: Task) -> str:
        return f"Completed '{task.description}' successfully. Definition of done satisfied: {task.definition_of_done}"


class UnreliableDemoWorker(AgentWorker):
    """Simulates a struggling agent -- for demonstrating demotion and escalation."""

    def __init__(self, failure_rate: float = 0.7, seed: int = 42):
        self._rng = random.Random(seed)
        self.failure_rate = failure_rate

    def run(self, task: Task) -> str:
        if self._rng.random() < self.failure_rate:
            return "Attempted the task but hit an error partway through."
        return f"Completed '{task.description}'."
