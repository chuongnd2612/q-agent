"""Durable queue for agent-driven live-PLANNING sessions (#900).

``testCaseMode="live-planner"`` runs the ``playwright-test-planner`` agent
against the real application before any test case exists, and feeds the plan it
brings back into the very next generation prompt. Until #900 that always ran on
the API host — the only live-browser action in the product that did not honour
``executionTarget="local-agent"``. On a local-agent deployment that is wrong
twice over: the API container often cannot reach the app under test at all, and
the manual login was captured on the *device's* browser profile, so the server
plans as an anonymous visitor and writes plausible test cases describing the
login screen. This table is the queue that lets the paired device plan instead.

Shaped after :class:`app.models.agent_authoring.AgentAuthoringSession`, not after
``agent_explore_service``'s in-process list, and the reason is sharper here than
it was for authoring: the planner's caller **waits inline** for the result (see
:func:`app.services.planner_agent_service.plan_ticket`). With more than one API
worker an in-memory queue would let the device's claim land on a worker that is
not the one waiting, so the waiter would time out on every single run while the
device happily planned. A table is the only shape where the enqueue and the
claim are guaranteed to see each other.

Unlike authoring there is no ``browser_driver`` column: the planner agent is
playwright-cli-native and the server hardcodes that driver for planning
(``plan_ticket``), so a per-session choice could only drift from it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime, timestamp_column

#: Lifecycle of one planning session.
#:
#: ``queued`` → ``running`` (claimed by the device) → ``done``/``failed`` (the
#: device posted back), or ``expired`` when the waiting server gave up first —
#: which is also what stops a device that starts polling late from opening a
#: browser for a plan nobody is waiting for any more.
PLANNING_SESSION_STATUSES = ("queued", "running", "done", "failed", "expired")

#: Statuses from which no further transition is expected.
TERMINAL_STATUSES = ("done", "failed", "expired")


class AgentPlanningSession(Base):
    """One live-planning session handed to the paired Local Agent."""

    __tablename__ = "agent_planning_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    #: The run being generated, so progress events can be relayed to its WebSocket.
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=True, index=True
    )

    project_key: Mapped[str] = mapped_column(String(200), default="")
    repo: Mapped[str] = mapped_column(String(200), default="")
    base_url: Mapped[str] = mapped_column(String(500), default="")
    origin: Mapped[str] = mapped_column(String(500), default="")
    #: Run code + ticket id: what the device labels its log lines with, and what
    #: identifies a stale session from a previous pass over the same ticket.
    run_code: Mapped[str] = mapped_column(String(64), default="")
    ticket: Mapped[str] = mapped_column(String(120), default="")
    sidecar_filename: Mapped[str] = mapped_column(String(200), default="plan.json")

    #: The planner agent definition's body, shipped as a system prompt string
    #: because the device's CLI has no ``--agent`` support (#894/#901).
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    task_prompt: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    max_budget_usd: Mapped[float] = mapped_column(Float, default=0.0)
    log_verbosity: Mapped[str] = mapped_column(String(24), default="concise")

    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    created_at: Mapped[datetime] = timestamp_column()
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, default=None)

    #: The plan sidecar's RAW TEXT as the device wrote it. Parsing and
    #: normalisation stay on the server (``planner_agent_service.normalize_plan``)
    #: so the device — which ships on its own release cadence — cannot fork that
    #: contract.
    plan_json: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
