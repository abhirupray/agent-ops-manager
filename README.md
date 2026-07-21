# Agent Ops Manager

Supervises AI agents the way an engineering manager supervises a team: autonomy is
**earned** through demonstrated performance, not assumed. Work-in-progress is
capped. Risky actions need sign-off until an agent has proven itself. Every
decision — every task assignment, every approval, every promotion or demotion — is
logged to an audit trail.

This is Phase 2 of a two-part project. [Phase 1](../escalation-agent) is a real
agent (Jira ticket + meeting-transcript triage); this repo is the governance layer
that supervises it — or any other agent that implements the same simple interface.

## Why this exists

Multiple 2026 enterprise AI surveys converge on the same finding: the blocker on
AI agent adoption isn't capability, it's trust and governance. 88% of agent pilots
never reach production. Over a third of companies say they couldn't immediately
"pull the plug" on a misbehaving agent. The idea of managing AI agents with the
same discipline you'd apply to a new hire — defined scope, a definition of done,
earned autonomy, an escalation path — has started showing up as engineering-
leadership thought leadership in 2026. This repo is a working implementation of
that idea, not just a description of it (see [DECISIONS.md](DECISIONS.md) for the
full reasoning behind every design choice, including this one).

## Architecture

```
                        ┌─────────────────────┐
   Task submitted  ───▶ │                      │
                        │      Supervisor       │
                        │  (src/core/supervisor)│
                        └──────────┬────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                     ▼
      WIP limit check      Autonomy check          Audit log
      (per agent cap)      (pre-approval needed?)  (every decision,
                                   │                 append-only)
                    ┌──────────────┴──────────────┐
                    ▼                              ▼
            Runs immediately              Queued for human approval
                    │                              │
                    ▼                    (approve) ▼  (reject)
            ┌───────────────┐          Runs immediately   Task rejected
            │  Worker agent  │
            │ (any AgentWorker)
            └───────┬────────┘
                    ▼
            Quality checker scores the output
            against the task's definition_of_done
                    │
        ┌────────────┴────────────┐
        ▼                          ▼
  Updates rolling quality    Low score at ANY autonomy
  history → may trigger      level still escalates for
  promotion/demotion          human review, post-hoc
  (every 5 completed tasks)
```

**The autonomy ladder:**

| Level | Meaning |
|---|---|
| L0 | Every task requires human approval before execution |
| L1 | Low/medium-risk tasks run automatically; high-risk still needs approval |
| L2 | Agent acts autonomously; every result is reviewed after the fact |
| L3 | Agent acts autonomously; only a sample of results is audited |
| L4 | Fully autonomous; audit only triggers on anomaly/escalation |

Agents start at a low level and are promoted or demoted every 5 completed tasks
based on their rolling average quality score — see [DECISIONS.md](DECISIONS.md)
for why it's a periodic cycle rather than a per-task adjustment.

**Trust-based routing:** instead of the caller picking an agent,
`supervisor.route_task(task)` staffs the work — it selects the most-trusted
eligible agent (scoped, unpaused, WIP headroom) for the task type. New agents get
an optimism prior so they aren't starved of work before they have a track record
(`src/core/router.py`).

**Human feedback loop:** `supervisor.record_human_feedback(task_id, corrected_score, note)`
lets a reviewer override an automated quality score. The correction replaces the
automated score in the agent's rolling trust history — so autonomy and routing end
up shaped by human judgment, not just the automated checker, and every correction
is in the audit trail (`src/core/feedback.py`). This is system-level learning
through interaction: the models stay stateless; the supervision gets better
calibrated with use.

## Setup

```bash
git clone <your-repo-url>
cd agent-ops-manager
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

No API key is required to run the tests, the demo script, or the dashboard against
the built-in demo agents — see below. An API key is only needed to register the
real Phase 1 escalation agent, or to use `LLMQualityChecker` instead of the default
heuristic scorer.

## Usage

**Run the tests** (no API key needed — all core logic is deterministic)
```bash
pytest tests/ -v
```

**Watch the full system run in one scripted demo** (promotion, demotion,
escalation, and the kill switch, in order — good for a recorded walkthrough)
```bash
python -m demo.run_demo
```

**Dashboard**
```bash
streamlit run app/streamlit_app.py
```

**API**
```bash
uvicorn src.api.main:app --reload
# GET  http://localhost:8000/agents
# POST http://localhost:8000/tasks/assign
# GET  http://localhost:8000/escalations
# POST http://localhost:8000/escalations/{task_id}/approve
# POST http://localhost:8000/agents/{agent_id}/pause   <- kill switch
```

**Docker (API + dashboard together)**
```bash
docker compose up --build
```

## Connecting the real escalation-agent service

As of v2.1, this is a real two-service setup: escalation-agent runs as its own
independent API, and this repo calls it over HTTP (see DECISIONS.md for why
this replaced an earlier direct-import approach, and what that approach cost).
Both services need to actually be running, in two separate terminals, each in
its own venv:

**Terminal 1 — escalation-agent:**
```bash
cd escalation-agent
source venv/bin/activate      # its own venv, its own dependencies
uvicorn src.api.main:app --port 8001
```

**Terminal 2 — agent-ops-manager:**
```bash
cd agent-ops-manager
source venv/bin/activate
# .env: ESCALATION_AGENT_URL=http://localhost:8001 (this is the default, only
# needed if escalation-agent runs somewhere else). If escalation-agent has
# ESCALATION_AGENT_API_KEY set, set the same value here too.
python scripts/run_governed_triage.py
```

This script pulls every open ticket dynamically from escalation-agent's live
`/tickets` endpoint (nothing hardcoded) and runs each one through the real
agent, governed by this supervisor: WIP limits, the autonomy ladder,
human-approval gating on high-risk tickets, quality scoring, and the audit
trail all apply. Add `--auto-approve` for a hands-off run.

The bootstrap module (`src/bootstrap.py`) health-checks escalation-agent at
startup and registers it as `jira-escalation-agent`, scoped to `ticket_triage`
tasks, starting at autonomy level L1 -- only if it's actually reachable.
Demo agents work regardless of whether escalation-agent is running.

To call it directly instead of via the script:
```python
from src.bootstrap import get_supervisor
from src.integrations.escalation_agent_http_worker import make_ticket_triage_task
from src.core import TaskRisk

supervisor = get_supervisor()
task = make_ticket_triage_task("PROJ-104", risk=TaskRisk.HIGH)
result = supervisor.assign_task("jira-escalation-agent", task)
```

## Writing your own worker agent

Any object with a `.run(task: Task) -> Any` method can be supervised — see
`src/core/worker.py` for the (deliberately minimal) protocol, and
`src/integrations/demo_worker.py` for the simplest possible examples.

## Extending this for a real deployment

1. Swap `HeuristicQualityChecker` for `LLMQualityChecker` (or your own) once you
   want real quality judgments, not just shape-checks.
2. Swap SQLite for a tamper-evident audit store if this needs to satisfy real
   compliance requirements (see DECISIONS.md).
3. Add true mid-execution cancellation to the kill switch if agents run
   asynchronously in your deployment (see DECISIONS.md for the current, stated
   limitation).
