"""
Streamlit dashboard. Run with: streamlit run app/streamlit_app.py

Framed deliberately like a manager's team view, not a generic monitoring
dashboard: a roster with role/autonomy/WIP, an escalation queue to
approve/reject, and a pause (kill switch) per agent.
"""
import os
import sys

import streamlit as st

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.bootstrap import get_supervisor
from src.core import Task, TaskRisk

st.set_page_config(page_title="Agent Ops Manager", page_icon="\U0001F9ED", layout="wide")

AUTONOMY_COLOR = {
    "L0_APPROVE_EVERY_ACTION": "#8A8F98",
    "L1_APPROVE_HIGH_RISK": "#4C7EA8",
    "L2_REVIEW_AFTER": "#3D8B5F",
    "L3_SAMPLED_AUDIT": "#2E9E6B",
    "L4_FULLY_AUTONOMOUS": "#1FAE73",
}

st.markdown(
    """
    <style>
    .stApp { background-color: #0D1117; }
    h1, h2, h3, p, span, label, .stMarkdown { color: #E6EDF3 !important; }
    .agent-card {
        background-color: #161B22;
        border: 1px solid #2A3138;
        border-left: 5px solid;
        border-radius: 6px;
        padding: 14px 18px;
        margin-bottom: 12px;
    }
    .autonomy-tag {
        font-size: 0.72rem; font-weight: 700; letter-spacing: 0.04em;
        padding: 3px 10px; border-radius: 4px; color: white;
    }
    .paused-tag {
        font-size: 0.72rem; font-weight: 700; letter-spacing: 0.04em;
        padding: 3px 10px; border-radius: 4px; background: #B3261E; color: white; margin-left: 8px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Agent Ops Manager")
st.caption(
    "Supervises AI agents the way an engineering manager supervises a team: autonomy is earned "
    "through demonstrated performance, WIP is capped, risky work needs sign-off, and every "
    "decision is logged."
)

supervisor = get_supervisor()

tab_roster, tab_escalations, tab_assign, tab_audit = st.tabs(
    ["Roster", "Escalation Queue", "Assign a Task", "Audit Trail"]
)

with tab_roster:
    st.subheader(f"Agents ({len(supervisor.agents)})")
    for agent in supervisor.agents.values():
        color = AUTONOMY_COLOR.get(agent.autonomy_level.name, "#4B5563")
        paused_html = '<span class="paused-tag">PAUSED (kill switch active)</span>' if agent.is_paused else ""
        avg_q = f"{agent.rolling_average_quality:.2f}" if agent.rolling_average_quality is not None else "—"
        html = f"""
        <div class="agent-card" style="border-left-color:{color};">
            <span class="autonomy-tag" style="background:{color};">{agent.autonomy_level.name}</span>{paused_html}
            <h4 style="margin:8px 0 2px 0;">{agent.agent_id}</h4>
            <p style="margin:0;color:#9AA5B1;">{agent.role}</p>
            <p style="margin:8px 0 0 0;">
                Scope: {', '.join(agent.allowed_task_types)} &nbsp;|&nbsp;
                WIP: {agent.current_wip}/{agent.wip_limit} &nbsp;|&nbsp;
                Completed: {agent.completed_task_count} &nbsp;|&nbsp;
                Rolling avg quality: {avg_q}
            </p>
        </div>
        """
        st.markdown(html, unsafe_allow_html=True)
        col1, col2 = st.columns([1, 1])
        with col1:
            if agent.is_paused:
                if st.button(f"Resume {agent.agent_id}", key=f"resume-{agent.agent_id}"):
                    supervisor.resume_agent(agent.agent_id)
                    st.rerun()
            else:
                if st.button(f"Pause {agent.agent_id} (kill switch)", key=f"pause-{agent.agent_id}"):
                    supervisor.pause_agent(agent.agent_id)
                    st.rerun()

with tab_escalations:
    pending = list(supervisor.pending_approvals.values())
    st.subheader(f"Pending human approval ({len(pending)})")
    if not pending:
        st.info("Nothing waiting on approval right now.")
    for task in pending:
        agent_id = supervisor._pending_agent_for_task[task.task_id]
        st.markdown(f"**{task.task_id}** — assigned to `{agent_id}` — risk: `{task.risk.value}`")
        st.write(task.description)
        st.caption(f"Definition of done: {task.definition_of_done}")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Approve", key=f"approve-{task.task_id}"):
                result = supervisor.approve_task(task.task_id)
                st.success(f"Executed. Quality score: {result.quality_score}")
                st.rerun()
        with c2:
            if st.button("Reject", key=f"reject-{task.task_id}"):
                supervisor.reject_task(task.task_id, reason="Rejected via dashboard")
                st.rerun()
        st.divider()

with tab_assign:
    st.subheader("Assign a task")
    agent_id = st.selectbox("Agent", list(supervisor.agents.keys()))
    agent = supervisor.agents[agent_id]
    task_type = st.selectbox("Task type", agent.allowed_task_types)
    description = st.text_input("Description", "Investigate and report back")
    definition_of_done = st.text_input("Definition of done", "A clear, evidence-backed result is returned")
    risk = st.selectbox("Risk level", [r.value for r in TaskRisk])
    if st.button("Assign task", type="primary"):
        task = Task(task_type=task_type, description=description,
                    definition_of_done=definition_of_done, risk=TaskRisk(risk))
        try:
            result = supervisor.assign_task(agent_id, task)
            st.success(f"Status: {result.status.value}")
            if result.quality_score is not None:
                st.write(f"Quality score: {result.quality_score} — {result.quality_reasoning}")
            if result.output:
                st.code(str(result.output))
        except Exception as e:
            st.error(str(e))

with tab_audit:
    st.subheader("Audit trail")
    filter_agent = st.selectbox("Filter by agent", ["All"] + list(supervisor.agents.keys()))
    events = supervisor.audit.query(agent_id=None if filter_agent == "All" else filter_agent, limit=100)
    for e in events:
        st.markdown(f"`{e['timestamp']}` **{e['event_type']}** — agent: `{e['agent_id']}` task: `{e['task_id']}`")
        if e["details"]:
            st.caption(str(e["details"]))
