import os

from src.core.audit import AuditLog
from src.core.state_store import StateStore
from src.core.models import AgentProfile, AutonomyLevel


def test_audit_log_file_is_deletable_immediately_after_use(tmp_path):
    """Regression test for a real bug found during manual verification on
    Windows: `with sqlite3.connect(...) as conn:` commits on exit but does
    NOT close the connection, which leaves the file locked. On Windows this
    surfaces as `PermissionError: ... being used by another process` when
    something tries to delete the db file right after. This test proves the
    file can be deleted immediately after every operation -- i.e. connections
    are actually being closed, not just committed."""
    db_path = str(tmp_path / "audit_close_test.db")
    audit = AuditLog(db_path=db_path)
    audit.log("SOME_EVENT", agent_id="agent-1", details={"x": 1})
    audit.query(agent_id="agent-1")

    os.remove(db_path)  # would raise PermissionError on Windows if a connection were still open
    assert not os.path.exists(db_path)


def test_state_store_file_is_deletable_immediately_after_use(tmp_path):
    db_path = str(tmp_path / "state_close_test.db")
    store = StateStore(db_path=db_path)
    store.save_agent(AgentProfile("agent-1", "Tester", ["demo_task"], AutonomyLevel.L0_APPROVE_EVERY_ACTION))
    store.load_agent("agent-1")

    os.remove(db_path)  # would raise PermissionError on Windows if a connection were still open
    assert not os.path.exists(db_path)
