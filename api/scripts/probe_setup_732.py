"""Seed a throwaway workspace for the #732 runtime probe.

Adds, on top of `app.seed`:
  * a probe user, with `owner_id` stamped on every seeded row (`owned()` filters
    strictly on owner_id == user.id, so unowned seed rows are invisible),
  * TWO projects — Surency Platform (ADO ticket source) and Claims Portal (Jira
    ticket source) — each with its own tickets, so the probe has a real negative
    control for scoping,
  * project_config for both, binding the TICKET SOURCE / CODE & KNOWLEDGE roles
    and leaving TEST CASE TARGET unset on one of them so the inherited default is
    visible on screen.

Prints `PLATFORM_GUID=...` / `CLAIMS_GUID=...` for the probe to consume.
"""

from __future__ import annotations

from app.db import SessionLocal, init_db, utcnow
from app.models.project import Project
from app.models.project_config import ProjectConfig
from app.models.provider_connection import ProviderConnection
from app.models.run import Run
from app.models.testcase import TestCase
from app.models.ticket import Ticket
from app.models.user import User
from app.seed import seed
from app.services import auth_service

EMAIL = "probe@example.com"
PASSWORD = "probe-password-123"


def main() -> None:
    seed()
    init_db()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == EMAIL).first()
        if user is None:
            user = User(
                email=EMAIL,
                first_name="Probe",
                last_name="User",
                role="admin",
                is_active=True,
                password_hash=auth_service.hash_password(PASSWORD),
            )
            db.add(user)
            db.flush()

        ado = db.query(ProviderConnection).filter(ProviderConnection.kind == "ado").first()
        jira = db.query(ProviderConnection).filter(ProviderConnection.kind == "jira").first()
        github = db.query(ProviderConnection).filter(ProviderConnection.kind == "github").first()

        platform = _ensure_project(db, "Surency Platform", "ado", user)
        claims = _ensure_project(db, "Claims Portal", "jira", user)

        _ensure_config(db, platform, user, ado.id, github.id, test_case_id=None)
        # Claims Portal binds an explicit TEST CASE TARGET so both states are on
        # screen at once: one inherited, one explicit.
        _ensure_config(db, claims, user, jira.id, github.id, test_case_id=ado.id)

        # Every seeded ticket belongs to Surency Platform; one distinctive ticket
        # goes to Claims Portal so "the other project's rows must not appear" is a
        # real assertion rather than a vacuous one.
        for ticket in db.query(Ticket).all():
            ticket.owner_id = user.id
            ticket.project_id = platform.id
            ticket.connection_id = ado.id
            ticket.provider_kind = "ado"
        db.flush()
        claims_ticket = db.query(Ticket).filter(Ticket.external_id == "CLM-9001").first()
        if claims_ticket is None:
            claims_ticket = Ticket(
                external_id="CLM-9001",
                provider_kind="jira",
                title="CLAIMS ONLY — must never show under Surency Platform",
                work_item_type="Story",
                status="Ready for QA",
                priority="High",
                assignee="Probe User",
                sprint="Sprint 24",
                synced_at=utcnow(),
            )
            db.add(claims_ticket)
        claims_ticket.owner_id = user.id
        claims_ticket.project_id = claims.id
        claims_ticket.connection_id = jira.id

        for model in (Run, TestCase, ProviderConnection):
            for row in db.query(model).all():
                if hasattr(row, "owner_id"):
                    row.owner_id = user.id
        for run in db.query(Run).all():
            run.project_guid = platform.guid
        db.commit()

        print(f"PLATFORM_GUID={platform.guid}")
        print(f"CLAIMS_GUID={claims.guid}")
    finally:
        db.close()


def _ensure_project(db, name: str, kind: str, user: User) -> Project:
    row = db.query(Project).filter(Project.name == name).first()
    if row is None:
        import uuid

        row = Project(
            guid=str(uuid.uuid4()),
            provider_kind=kind,
            external_id=name,
            name=name,
            active=True,
            meta={},
        )
        db.add(row)
        db.flush()
    row.owner_id = user.id
    if not row.guid:
        import uuid

        row.guid = str(uuid.uuid4())
    db.flush()
    return row


def _ensure_config(db, project: Project, user: User, wi_id, repo_id, test_case_id) -> None:
    row = (
        db.query(ProjectConfig)
        .filter(ProjectConfig.key == project.name, ProjectConfig.owner_id == user.id)
        .first()
    )
    if row is None:
        row = ProjectConfig(key=project.name, name=project.name, owner_id=user.id)
        db.add(row)
    row.project_guid = project.guid
    row.work_item_connection_id = wi_id
    row.repository_connection_id = repo_id
    row.test_case_connection_id = test_case_id
    row.base_url = "https://staging.surency.example"
    db.flush()


if __name__ == "__main__":
    main()
