"""
Tests for the HTTP-based escalation-agent integration.

These use `responses` (a requests-mocking library) rather than requiring a
live escalation-agent process -- CI shouldn't need to boot a second service
just to verify retry/error-handling logic. A separate, smaller live test is
skipped unless escalation-agent is actually running, for use during manual
verification.
"""
import os

import pytest
import responses

from src.core import TaskRisk
from src.integrations.escalation_agent_http_worker import (
    EscalationAgentHTTPWorker,
    EscalationServiceUnavailable,
    get_open_ticket_keys,
    is_escalation_agent_reachable,
    make_ticket_triage_task,
)

BASE_URL = "http://localhost:8001"


@pytest.fixture(autouse=True)
def _use_default_base_url(monkeypatch):
    monkeypatch.delenv("ESCALATION_AGENT_URL", raising=False)
    monkeypatch.delenv("ESCALATION_AGENT_API_KEY", raising=False)


# -- Health check -----------------------------------------------------------------

@responses.activate
def test_reachable_when_health_check_succeeds():
    responses.add(responses.GET, f"{BASE_URL}/health", json={"status": "ok"}, status=200)
    assert is_escalation_agent_reachable() is True


def test_not_reachable_when_connection_refused(monkeypatch):
    # Point at a port nothing listens on, rather than assuming the default
    # port (8001) is free -- that assumption broke during manual live
    # verification, when a real escalation-agent instance WAS running on
    # 8001. Explicit is more robust than "probably nothing's there."
    monkeypatch.setenv("ESCALATION_AGENT_URL", "http://localhost:8199")
    assert is_escalation_agent_reachable() is False


# -- Successful triage call ---------------------------------------------------------

@responses.activate
def test_worker_calls_triage_endpoint_and_parses_response():
    responses.add(
        responses.POST, f"{BASE_URL}/triage/PROJ-104",
        json={
            "ticket_key": "PROJ-104", "risk_level": "NEEDS_ESCALATION",
            "reasoning": "Broken promise.", "evidence": ["June 15: slipped again."],
            "recommended_action": "Escalate to infra lead.",
        },
        status=200,
    )
    worker = EscalationAgentHTTPWorker()
    task = make_ticket_triage_task("PROJ-104", risk=TaskRisk.HIGH)
    output = worker.run(task)
    assert "NEEDS_ESCALATION" in output
    assert "Broken promise." in output


# -- Error handling -----------------------------------------------------------------

@responses.activate
def test_worker_raises_clear_error_on_401():
    responses.add(responses.POST, f"{BASE_URL}/triage/PROJ-104", json={"detail": "Invalid API key."}, status=401)
    worker = EscalationAgentHTTPWorker()
    with pytest.raises(PermissionError):
        worker.run(make_ticket_triage_task("PROJ-104"))


@responses.activate
def test_worker_raises_clear_error_on_404():
    responses.add(responses.POST, f"{BASE_URL}/triage/PROJ-999", json={"detail": "not found"}, status=404)
    worker = EscalationAgentHTTPWorker()
    with pytest.raises(ValueError):
        worker.run(make_ticket_triage_task("PROJ-999"))


def test_worker_raises_service_unavailable_when_unreachable(monkeypatch):
    monkeypatch.setenv("ESCALATION_AGENT_URL", "http://localhost:8199")
    monkeypatch.setattr("src.integrations.escalation_agent_http_worker.time.sleep", lambda s: None)
    worker = EscalationAgentHTTPWorker()
    with pytest.raises(EscalationServiceUnavailable):
        worker.run(make_ticket_triage_task("PROJ-104"))


# -- Retry behavior -----------------------------------------------------------------

@responses.activate
def test_retries_transient_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr("src.integrations.escalation_agent_http_worker.time.sleep", lambda s: None)
    responses.add(responses.POST, f"{BASE_URL}/triage/PROJ-104", status=503)
    responses.add(responses.POST, f"{BASE_URL}/triage/PROJ-104", status=503)
    responses.add(
        responses.POST, f"{BASE_URL}/triage/PROJ-104",
        json={"ticket_key": "PROJ-104", "risk_level": "ON_TRACK", "reasoning": "x",
              "evidence": [], "recommended_action": "x"},
        status=200,
    )
    worker = EscalationAgentHTTPWorker()
    output = worker.run(make_ticket_triage_task("PROJ-104"))
    assert "ON_TRACK" in output
    assert len(responses.calls) == 3


# -- Dynamic ticket fetching (nothing hardcoded) -------------------------------------

@responses.activate
def test_get_open_ticket_keys_returns_dynamic_list():
    responses.add(
        responses.GET, f"{BASE_URL}/tickets",
        json=[
            {"key": "PROJ-101", "summary": "x", "status": "In Progress", "priority": "Medium",
             "age_days": 1, "days_since_update": 1},
            {"key": "PROJ-104", "summary": "x", "status": "Blocked", "priority": "Critical",
             "age_days": 20, "days_since_update": 12},
        ],
        status=200,
    )
    keys = get_open_ticket_keys()
    assert keys == [("PROJ-101", "Medium"), ("PROJ-104", "Critical")]


# -- Live integration test (opt-in, needs a real running escalation-agent) ----------

live_reachable = is_escalation_agent_reachable()


@pytest.mark.skipif(not live_reachable, reason="escalation-agent is not running -- start it to run this test")
def test_live_health_check():
    assert is_escalation_agent_reachable() is True
