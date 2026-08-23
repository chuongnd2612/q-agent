"""SQLAlchemy models for Q-Agent. Import order registers them on Base.metadata."""

from app.models.provider import Provider
from app.models.provider_connection import ProviderConnection
from app.models.project import Project
from app.models.project_config import ProjectConfig
from app.models.ticket import Ticket
from app.models.run import Run, RunTicket
from app.models.testcase import AutomationSpec, TestCase
from app.models.execution import Evidence, Execution, ExecutionResult
from app.models.report import Report
from app.models.comment import TicketComment
from app.models.knowledge import ProjectKnowledge
from app.models.linked import LinkedTestCase
from app.models.audit import AuditLog
from app.models.claude_usage import ClaudeUsage
from app.models.claude_credentials import ClaudeCredentials
from app.models.user import User
from app.models.session import Session
from app.models.agent_device import AgentDevice
from app.models.automation_project import AutomationFile, AutomationProject
from app.models.agent_authoring import AgentAuthoringSession

__all__ = [
    "AuditLog",
    "ClaudeUsage",
    "ClaudeCredentials",
    "User",
    "Session",
    "AgentDevice",
    "AgentAuthoringSession",
    "AutomationProject",
    "AutomationFile",
    "Provider",
    "ProviderConnection",
    "Project",
    "ProjectConfig",
    "Ticket",
    "Run",
    "RunTicket",
    "TestCase",
    "AutomationSpec",
    "Execution",
    "ExecutionResult",
    "Evidence",
    "Report",
    "TicketComment",
    "ProjectKnowledge",
    "LinkedTestCase",
]
