"""
HTTP-based adapter for the real escalation agent.

This replaced an earlier version of this file that imported escalation-agent's
Python code directly (loading its `src` package in-process). That approach
worked but had real costs, discovered during manual verification (see
DECISIONS.md for the full account):
  - agent-ops-manager's venv needed ALL of escalation-agent's dependencies
    installed too (langgraph, chromadb, langchain-anthropic...) just to call
    one function
  - both repos happen to have a top-level package literally named `src`,
    which silently collided in-process and needed a workaround
  - the two repos could never run on different machines -- they had to share
    a filesystem and a Python environment

This version calls escalation-agent's REST API instead -- a real network
request, the same pattern used to connect any two independently-owned
services. agent-ops-manager now needs nothing from escalation-agent except
its network address; escalation-agent needs nothing from agent-ops-manager at
all. Either can be deployed, scaled, or replaced independently.

Requires ESCALATION_AGENT_URL (default http://localhost:8001) and, if
escalation-agent has auth configured, ESCALATION_AGENT_API_KEY.
"""
import logging
import os
import random
import time

import requests

from ..core.models import Task, TaskRisk
from ..core.worker import AgentWorker

logger = logging.getLogger("agent_ops.escalation_agent_client")

DEFAULT_BASE_URL = "http://localhost:8001"
REQUEST_TIMEOUT_SECONDS = 60  # agent investigations involve multiple LLM calls; generous on purpose
MAX_ATTEMPTS = 3
BASE_RETRY_DELAY = 2.0
TRANSIENT_STATUS_CODES = {429, 502, 503, 504}


class EscalationServiceUnavailable(Exception):
    """Raised when escalation-agent's API can't be reached at all -- distinct
    from a normal 404/error response, which means it WAS reached."""


def _base_url() -> str:
    return os.environ.get("ESCALATION_AGENT_URL", DEFAULT_BASE_URL).rstrip("/")


def _headers() -> dict:
    key = os.environ.get("ESCALATION_AGENT_API_KEY")
    return {"X-API-Key": key} if key else {}


def is_escalation_agent_reachable() -> bool:
    """Health check used at registration time to decide whether to add this
    worker to the supervisor's roster at all."""
    try:
        resp = requests.get(f"{_base_url()}/health", timeout=5)
        return resp.status_code == 200
    except requests.exceptions.RequestException:
        return False


def _call_with_retries(method: str, url: str, **kwargs) -> requests.Response:
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.request(method, url, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
            if resp.status_code in TRANSIENT_STATUS_CODES and attempt < MAX_ATTEMPTS:
                raise requests.exceptions.RequestException(f"Transient status {resp.status_code}")
            return resp
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt == MAX_ATTEMPTS:
                raise EscalationServiceUnavailable(
                    f"Could not reach escalation-agent at {url} after {MAX_ATTEMPTS} attempts: {e}. "
                    f"Is it running? Start it with: uvicorn src.api.main:app --port 8001 "
                    f"(from the escalation-agent directory, in ITS OWN venv)."
                ) from e
            delay = BASE_RETRY_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1)
            logger.warning("Transient error calling escalation-agent (attempt %d/%d): %s -- retrying in %.1fs",
                            attempt, MAX_ATTEMPTS, e, delay)
            time.sleep(delay)
    raise EscalationServiceUnavailable(str(last_error))  # pragma: no cover -- unreachable


class EscalationAgentHTTPWorker(AgentWorker):
    """Wraps escalation-agent's real triage endpoint as a supervised worker.
    Each Task's payload must contain {"ticket_key": "PROJ-104"}."""

    def run(self, task: Task) -> str:
        ticket_key = task.payload.get("ticket_key")
        if not ticket_key:
            raise ValueError("EscalationAgentHTTPWorker requires task.payload['ticket_key'].")

        url = f"{_base_url()}/triage/{ticket_key}"
        resp = _call_with_retries("POST", url, headers=_headers())

        if resp.status_code == 401:
            raise PermissionError(
                "escalation-agent rejected the request (401). Check ESCALATION_AGENT_API_KEY "
                "matches the ESCALATION_AGENT_API_KEY set in escalation-agent's own .env."
            )
        if resp.status_code == 404:
            raise ValueError(f"escalation-agent has no ticket with key '{ticket_key}'.")
        resp.raise_for_status()

        assessment = resp.json()
        return (
            f"risk_level={assessment['risk_level']}\n"
            f"reasoning={assessment['reasoning']}\n"
            f"evidence={assessment['evidence']}\n"
            f"recommended_action={assessment['recommended_action']}"
        )


def make_ticket_triage_task(ticket_key: str, risk: TaskRisk = TaskRisk.MEDIUM) -> Task:
    """Convenience constructor for a ticket-triage Task, matching what the
    supervisor expects for this integration."""
    return Task(
        task_type="ticket_triage",
        description=f"Investigate and assess Jira ticket {ticket_key} for escalation risk.",
        definition_of_done=(
            "Return a risk level (ON_TRACK/STALE/AT_RISK/NEEDS_ESCALATION) backed by evidence "
            "from ticket comments and/or meeting transcripts, plus a concrete recommended action."
        ),
        payload={"ticket_key": ticket_key},
        risk=risk,
    )


def get_open_ticket_keys() -> list[tuple[str, str]]:
    """Returns [(ticket_key, priority), ...] for every open ticket, fetched
    over HTTP from escalation-agent -- this is what lets a caller enumerate
    ALL tickets dynamically instead of hardcoding ticket keys. Used by
    scripts/run_governed_triage.py."""
    url = f"{_base_url()}/tickets"
    resp = _call_with_retries("GET", url, headers=_headers())
    resp.raise_for_status()
    return [(t["key"], t["priority"]) for t in resp.json()]
