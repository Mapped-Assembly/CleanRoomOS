"""Typed domain records for the persistent CleanRoomOS runtime."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
RecordKind = Literal[
    "agent",
    "task",
    "action",
    "risk_decision",
    "sop_reference",
    "incident",
    "operator_intervention",
    "maintenance",
    "calibration",
]


class DomainModel(BaseModel):
    """Base model that rejects unknown fields and validates assignment."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class CleanroomInstanceCreate(DomainModel):
    """Create one persistent cleanroom/environment boundary."""

    id: Identifier | None = None
    name: Text
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class CleanroomInstanceRead(DomainModel):
    """Read model for a persisted cleanroom/environment instance."""

    id: Identifier
    name: Text
    attributes: dict[str, JsonValue]
    created_at: datetime
    updated_at: datetime


class AgentPayload(DomainModel):
    """Persistent identity and coarse runtime state for an agent or robot."""

    name: Text
    status: Text


class TaskPayload(DomainModel):
    """Persistent task state associated with a cleanroom instance."""

    title: Text
    status: Text
    assigned_agent_id: Identifier | None = None


class ActionPayload(DomainModel):
    """Minimal action state; provenance is expanded by issue #23."""

    name: Text
    status: Text
    task_id: Identifier | None = None
    target_id: Identifier | None = None


class RiskDecisionPayload(DomainModel):
    """Persisted risk decision produced for an operational action."""

    risk_level: Literal["none", "low", "medium", "high"]
    decision: Literal["allow", "block", "escalate"]
    reason: Text


class SOPReferencePayload(DomainModel):
    """Reference a versioned SOP section used by runtime decisions."""

    document_id: Identifier
    version: Text
    locator: Text


class IncidentPayload(DomainModel):
    """Persistent incident state for later inspection and linkage."""

    summary: Text
    severity: Literal["low", "medium", "high", "critical"]
    status: Literal["open", "resolved"]


class OperatorInterventionPayload(DomainModel):
    """Record a human intervention without treating it as agent memory."""

    operator_id: Identifier
    action: Text
    reason: Text


class MaintenancePayload(DomainModel):
    """Persistent maintenance state for a device or robot."""

    equipment_id: Identifier
    status: Literal["current", "due", "overdue", "out_of_service"]
    performed_at: datetime | None = None
    notes: Text | None = None


class CalibrationPayload(DomainModel):
    """Persistent calibration state for a device or robot."""

    equipment_id: Identifier
    status: Literal["valid", "due", "expired", "unknown"]
    calibrated_at: datetime | None = None
    due_at: datetime | None = None


class _RecordCreateBase(DomainModel):
    """Shared fields for typed operational record creation."""

    id: Identifier | None = None


class AgentRecordCreate(_RecordCreateBase):
    """Create an agent/robot record."""

    kind: Literal["agent"] = "agent"
    payload: AgentPayload


class TaskRecordCreate(_RecordCreateBase):
    """Create a task record."""

    kind: Literal["task"] = "task"
    payload: TaskPayload


class ActionRecordCreate(_RecordCreateBase):
    """Create an action record."""

    kind: Literal["action"] = "action"
    payload: ActionPayload


class RiskDecisionRecordCreate(_RecordCreateBase):
    """Create a risk decision record."""

    kind: Literal["risk_decision"] = "risk_decision"
    payload: RiskDecisionPayload


class SOPReferenceRecordCreate(_RecordCreateBase):
    """Create a versioned SOP reference record."""

    kind: Literal["sop_reference"] = "sop_reference"
    payload: SOPReferencePayload


class IncidentRecordCreate(_RecordCreateBase):
    """Create an incident record."""

    kind: Literal["incident"] = "incident"
    payload: IncidentPayload


class OperatorInterventionRecordCreate(_RecordCreateBase):
    """Create an operator intervention record."""

    kind: Literal["operator_intervention"] = "operator_intervention"
    payload: OperatorInterventionPayload


class MaintenanceRecordCreate(_RecordCreateBase):
    """Create a maintenance state/event record."""

    kind: Literal["maintenance"] = "maintenance"
    payload: MaintenancePayload


class CalibrationRecordCreate(_RecordCreateBase):
    """Create a calibration state/event record."""

    kind: Literal["calibration"] = "calibration"
    payload: CalibrationPayload


OperationalRecordCreate: TypeAlias = Annotated[
    AgentRecordCreate
    | TaskRecordCreate
    | ActionRecordCreate
    | RiskDecisionRecordCreate
    | SOPReferenceRecordCreate
    | IncidentRecordCreate
    | OperatorInterventionRecordCreate
    | MaintenanceRecordCreate
    | CalibrationRecordCreate,
    Field(discriminator="kind"),
]


class OperationalRecordRead(DomainModel):
    """Read model for any persisted instance-scoped operational record."""

    id: Identifier
    instance_id: Identifier
    kind: RecordKind
    payload: dict[str, JsonValue]
    created_at: datetime
    updated_at: datetime
