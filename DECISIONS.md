# Engineering Decisions

## Four real bugs found during manual verification (before you assume anything works, verify it)

**1. `.env` was never actually loaded.** `python-dotenv` was a listed dependency
in both repos, but the code never called `load_dotenv()` -- so a correctly
filled `.env` file was silently ignored by plain `python` invocations (some
IDEs auto-load `.env` and masked this during earlier testing). Fixed by calling
`load_dotenv()` in `src/bootstrap.py` (the single entry point everything else --
API, dashboard, REPL -- goes through) and in escalation-agent's `src/config.py`.

**2. A `src`/`src` namespace collision broke the cross-repo integration.** Both
repos have a top-level package literally named `src`. `escalation_agent_adapter.py`
originally did `sys.path.insert(...)` then `from src.agent import assess_ticket`
-- but by the time that runs, THIS process has already imported ITS OWN `src`
package. Python finds `src` already in `sys.modules` and reuses it; it does not
re-search `sys.path` for a second, different package of the same name. Result:
`ModuleNotFoundError: No module named 'src.agent'`, even with escalation-agent
correctly present as a sibling directory. This is a real, non-obvious Python
import-system behavior, not a typo.

Fix: `escalation_agent_adapter.py` now loads escalation-agent's `src` package
under a distinct name (`escalation_agent_src`) using `importlib.util`, so both
same-named packages coexist in one process without colliding. Regression tests
in `tests/test_escalation_agent_integration.py` prove the import succeeds --
and skip gracefully (not fail) in CI, where only this repo is checked out and
the sibling genuinely isn't present.

**3. Missing cross-repo dependencies gave a confusing raw traceback.** Each
repo has its own venv with only its own dependencies. When agent-ops-manager's
adapter loads escalation-agent's real code, that code needs escalation-agent's
dependencies (`langgraph`, `langchain-anthropic`, `chromadb`, etc.) installed
in agent-ops-manager's venv too, since agent-ops-manager's process is the one
executing it. The original failure mode was a raw `ModuleNotFoundError` several
frames deep in someone else's code -- correct, but not actionable at a glance.
Fixed: the adapter now catches this specific case and re-raises with the exact
fix (`pip install -r <path-to-escalation-agent>/requirements.txt`) in the
message. The regression test for this needed its own fix mid-development too:
an early version mutated global `sys.modules` state and leaked a corrupted
cache into the next test in the file -- a reminder that tests exercising
process-global state (module caches, singletons) need explicit save/restore,
not just a mock and an assertion.

**The broader lesson, worth stating in an interview:** two independently-built
Python repos sharing a package name is a landmine that only surfaces at
integration time -- unit tests in each repo alone will never catch it, because
each repo only ever imports its own `src`. This is exactly the class of bug
that manual, cross-system verification exists to catch, and exactly the
argument for not skipping it before a GitHub push.


**4. SQLite connections weren't closing (Windows-only symptom).** Found while
running `demo/run_demo.py` on Windows: the script's own cleanup step
(`os.remove(db_path)`) failed with `PermissionError: ... being used by another
process`. Root cause: `AuditLog` and `StateStore` used
`with sqlite3.connect(...) as conn:` throughout. That pattern is a common trap --
Python's sqlite3 connection context manager **commits or rolls back the
transaction on exit, but does not close the connection**. The connection stayed
open until garbage collected, which held a file lock. POSIX (Mac/Linux) allows
deleting a file that's still open elsewhere, so this was invisible there; Windows
enforces the lock, so it surfaced immediately.

Fix: both classes now use a small `@contextmanager` wrapper
(`StateStore._connect`, and the equivalent in `AssessmentMemory` in the
escalation-agent repo) that explicitly commits *and* closes in a `finally` block,
so a connection can never outlive the `with` block regardless of exceptions.
Regression tests (`tests/test_connection_cleanup.py`) assert the db file is
immediately deletable after use -- the exact operation that failed originally.

This is left in as a case study, not smoothed over: it's a real example of a
platform-specific bug that automated tests alone (all originally passing on
Linux in CI) did not catch, and that only manual, cross-platform verification
found. That's precisely the argument for the manual verification pass this
project's roadmap insists on before any GitHub push.

## v2.1: From direct import to a real service boundary (HTTP)

The Phase 1 integration originally worked by loading escalation-agent's Python
code directly in-process (see the "Four real bugs" section below for the full
account of what that cost in practice: shared-venv dependency requirements, a
`src`/`src` package name collision, and the two repos being unable to run on
separate machines). This version replaces that with a real network boundary:
`src/integrations/escalation_agent_http_worker.py` calls escalation-agent's
REST API (`POST /triage/{ticket_key}`, `GET /tickets`) over HTTP, with retry
and backoff on transient failures, exactly the way any two independently-owned
services in a real company talk to each other.

**What this actually fixes, concretely:** agent-ops-manager's dependency list
dropped `pandas`, `numpy`, `scikit-learn`, `chromadb`, `langchain-core`, and
`langgraph` entirely -- it now needs nothing from escalation-agent except its
network address (`ESCALATION_AGENT_URL`) and, if configured, an API key. Both
services can run on different machines, be deployed independently, scale
independently, and be owned by different teams without ever touching each
other's code or environment. escalation-agent's API also gained its own
authentication (`src/api/auth.py` in that repo) and now returns a clean 503 --
not a raw 500 -- when it's misconfigured (e.g. missing its own API key),
discovered and fixed during this same manual, live verification pass.

**Trade-off, stated honestly:** this adds real operational overhead --
both services must actually be running for the integration to work, versus
one process doing everything. That's the correct trade-off here: the
integration is meant to model a real multi-service platform, not minimize
local dev friction. `is_escalation_agent_reachable()` and the retry/backoff
logic exist specifically to make that overhead safe rather than silent.


Version 2.0 responds to a deliberate production-readiness audit of v1. The three
findings that mattered most, and their resolutions:

**1. State now survives restarts and is shared across processes.** In v1, agent
profiles, pending approvals, and results lived in process memory -- restart meant
amnesia, and the API and dashboard processes each held divergent state
(split-brain). v2 introduces a write-through persistence layer
(`src/core/state_store.py`, repository pattern over SQLite): every mutation is
persisted inside the mutating call, and `register_agent` hydrates persisted state
(earned autonomy, trust history, pause flag) over code defaults on startup --
because earned trust must survive restarts; code-level defaults are only for
first boot. Why SQLite and not Postgres: single-node platform, zero-ops, and the
repository interface is the seam where Postgres slots in later -- swapping
engines changes one file. Why no ORM: simple queries, small schema; SQLAlchemy
would add indirection with no current benefit. Why write-through and not
write-behind: simple, crash-safe, correct at this throughput.

**2. The API is authenticated with role scoping.** API keys with two RBAC roles
(ADMIN: lifecycle + assignment; REVIEWER: read + approve/reject + feedback),
constant-time key comparison, keys from environment variables. Why API keys and
not JWT: this is a service/platform API, not a user session system -- Stripe,
Anthropic, and OpenAI all authenticate their platform APIs with keys. JWT earns
its complexity with multi-user sessions and an identity provider; adding it here
would be resume-driven engineering. The upgrade path (token endpoint or SSO) is
clear if multi-user arrives. Dev mode (no keys configured) still works but logs a
prominent warning -- the demo stays runnable, and an unsecured deployment is loud
about being unsecured rather than silently open.

**3. Proper packaging.** `pyproject.toml` with pytest `pythonpath` config
replaced the per-file `sys.path` hacks in tests. The API also gained explicit
Pydantic response models (internal dataclasses no longer leak through the
boundary via `__dict__`), locked-down CORS (deny-all unless `ALLOWED_ORIGINS` is
set), structured application logging separate from the domain audit trail, and a
trust-routed assignment mode (`POST /tasks/assign` without an `agent_id`).


## The core idea, and where it comes from

This project operationalizes an idea that's currently circulating as engineering-
leadership thought leadership (see e.g. the "manage AI agents like junior engineers"
framing that gained traction in early 2026 — definition-of-done discipline, WIP
limits, delegation ladders) rather than something invented from nothing here. What
this repo contributes is a working implementation of that idea as actual
infrastructure: a supervisor that enforces those principles in code, not just in a
blog post. That distinction is deliberate — see the README for the fuller context.

## Why autonomy is a 5-level ladder, adjusted on a periodic review cycle

Autonomy (`AutonomyLevel`, `src/core/models.py`) determines whether a task needs
human sign-off before execution. It's adjusted by `maybe_adjust_autonomy`
(`src/core/policy.py`) only every `EVALUATION_WINDOW` (5) completed tasks, not
after every single task.

The alternative — adjusting after every task — would cause an agent to bounce
between autonomy levels after one good or bad result, which doesn't reflect how
trust actually works and would make the audit log noisy and hard to reason about.
A periodic cycle mirrors an actual performance-review cadence: enough data points
to be a real signal, not a knee-jerk reaction to one outcome.

## Why quality checking is pluggable, and why the default is a heuristic, not an LLM

Every task carries a mandatory `definition_of_done`, which is what makes automated
scoring possible without a human reading every result. `HeuristicQualityChecker`
(the default) is a fast, free, deterministic shape-check — it exists specifically so
the supervisor's core logic (WIP limits, approval routing, promotion/demotion,
escalation) can be fully unit-tested without an API key, which is also why the test
suite in this repo needs no `ANTHROPIC_API_KEY` to pass. `LLMQualityChecker` is the
production-grade option that actually judges whether output satisfies the
definition of done — swap it in via the `quality_checker` argument to `Supervisor`.

## Why the kill switch only blocks new task assignment

`Supervisor.pause_agent()` prevents an agent from being assigned any new task. It
does **not** interrupt a task already mid-execution. This is a real, stated
limitation, not an oversight: the current execution model is synchronous
(`worker.run(task)` blocks until it returns), so there's no natural interruption
point mid-call. A production version running agents as cancellable async tasks or
separate processes could add true mid-execution termination; this repo's honest
scope is "stops the bleeding immediately for all future work," which is still the
majority of what the enterprise AI governance research this project responds to
is actually asking for (the ability to stop an agent from taking further action).

## Why post-hoc escalation exists even at high autonomy levels

A task can still end up `ESCALATED` even when the agent's autonomy level didn't
require pre-approval (see `_execute` in `src/core/supervisor.py`): if the quality
score comes back below `POST_HOC_ESCALATION_THRESHOLD`, the result is flagged for
human review regardless of autonomy level. Autonomy controls *who reviews before
execution*, not *whether bad results get caught at all* — a fully autonomous agent
that produces a bad result still surfaces it, it just doesn't block on it first.

## Why the audit trail is SQLite, and is append-only

`AuditLog` (`src/core/audit.py`) uses SQLite directly rather than an ORM or a
hosted database. For a portfolio-scale system this is the right footprint —
zero setup, a real persistent file, fully queryable with SQL if needed — and the
`INSERT`-only access pattern (no `UPDATE`/`DELETE` anywhere in this codebase) is
what makes it a meaningful audit trail rather than just an event bus. A production
deployment handling real compliance requirements would want an actually
tamper-evident store (e.g. an append-only log with hash chaining, or a managed
audit-log service); that's a genuine gap between this repo and a production system,
stated plainly.

## Why the Phase 1 integration is over HTTP, not a package dependency

`src/integrations/escalation_agent_http_worker.py` calls escalation-agent's own
REST API rather than declaring it as an installable pip dependency or importing
its code directly. See "v2.1: From direct import to a real service boundary"
above for the full account, including the direct-import approach this replaced
and exactly what broke because of it. Short version: this keeps both repos
independently cloneable, runnable, deployable, and testable entirely on their
own -- which matters both for two separate portfolio projects and for how real
companies actually connect independently-owned services -- while still proving
a real, live integration exists between them, not just a described one.

## Known limitations, stated plainly

- The heuristic quality checker is intentionally shallow (see above) — it is a
  testing and demo mechanism, not a real quality judge. `LLMQualityChecker` is the
  one to use for anything that should actually reflect task quality.
- The kill switch does not interrupt in-flight execution (see above).
- The audit trail is not tamper-evident (no hash chaining / signing) — it's
  append-only by convention (no delete/update code path exists), not by database-
  level enforcement.
- `EscalationAgentHTTPWorker` has been verified end-to-end against a real, live
  escalation-agent instance running as its own process (not just unit-tested
  against a mock) — retry/backoff, dynamic ticket fetching, and error handling
  were all exercised against the real HTTP service during manual verification.
  Not yet verified: a full triage run with a real `ANTHROPIC_API_KEY` configured
  on escalation-agent's side, producing real (not config-error) risk assessments
  through the governed pipeline end to end — that's the next manual check.
