"""
The only contract a worker agent needs to satisfy to be supervised. This is
deliberately minimal -- the supervisor doesn't care whether a worker is a
LangGraph agent, a single API call, or a rule-based script, only that it
takes a Task and returns some output.
"""
from typing import Any, Protocol

from .models import Task


class AgentWorker(Protocol):
    def run(self, task: Task) -> Any:
        """Execute the task and return raw output. Raise an exception on failure --
        the supervisor catches it and records a FAILED result."""
        ...
