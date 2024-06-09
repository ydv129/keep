from datetime import datetime
from typing import List, Optional

from sqlalchemy import TEXT, String
from sqlmodel import JSON, Column, Field, Relationship, SQLModel, UniqueConstraint


class Workflow(SQLModel, table=True):
    id: str = Field(default=None, primary_key=True, max_length=36)
    tenant_id: str = Field(foreign_key="tenant.id", max_length=36)
    name: str = Field(max_length=1024)
    description: Optional[str]
    created_by: str = Field(max_length=1024)
    updated_by: Optional[str] = None
    creation_time: datetime = Field(default_factory=datetime.utcnow)
    interval: Optional[int]
    workflow_raw: str = Field(sa_column=Column(TEXT))
    is_deleted: bool = Field(default=False)
    revision: int = Field(default=1, nullable=False)
    last_updated: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        orm_mode = True


class WorkflowExecution(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("workflow_id", "execution_number", "is_running", "timeslot"),
    )

    id: str = Field(default=None, primary_key=True, max_length=36)
    workflow_id: str = Field(foreign_key="workflow.id", max_length=36)
    tenant_id: str = Field(foreign_key="tenant.id", max_length=36)
    started: datetime = Field(default_factory=datetime.utcnow)
    triggered_by: str = Field(max_length=1024)
    status: str = Field(max_length=1024)
    is_running: int = Field(default=1)
    timeslot: int = Field(
        default_factory=lambda: int(datetime.utcnow().timestamp() / 120)
    )
    execution_number: int
    logs: Optional[str]
    error: Optional[str] = Field(max_length=8000)
    execution_time: Optional[int]
    results: dict = Field(sa_column=Column(JSON), default={})

    logs: List["WorkflowExecutionLog"] = Relationship(
        back_populates="workflowexecution"
    )
    workflow_to_alert_execution: "WorkflowToAlertExecution" = Relationship(
        back_populates="workflow_execution"
    )

    class Config:
        orm_mode = True


class WorkflowToAlertExecution(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("workflow_execution_id", "alert_fingerprint"),)

    # https://sqlmodel.tiangolo.com/tutorial/automatic-id-none-refresh/
    id: Optional[int] = Field(primary_key=True, default=None)
    workflow_execution_id: str = Field(
        foreign_key="workflowexecution.id", max_length=36
    )
    alert_fingerprint: str = Field(max_length=256)
    event_id: str | None = Field(max_length=256, nullable=True)
    workflow_execution: WorkflowExecution = Relationship(
        back_populates="workflow_to_alert_execution"
    )


class WorkflowExecutionLog(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    workflow_execution_id: str = Field(
        foreign_key="workflowexecution.id", max_length=36
    )
    timestamp: datetime
    message: str = Field(sa_column=String(length=10240))
    workflowexecution: Optional[WorkflowExecution] = Relationship(back_populates="logs")
    context: dict = Field(sa_column=Column(JSON))

    class Config:
        orm_mode = True
