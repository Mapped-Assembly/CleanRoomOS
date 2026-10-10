"""Contracts for auditable actions and versioned operational policy."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, JsonValue

from cleanroom_os.domain import (
    ActionEvidence,
    DomainModel,
    Identifier,
    OperationalRecordRead,
    SOPReferencePayload,
    Text,
)

Decision = Literal["allow", "block", "escalate"]
RiskLevel = Literal["none", "low", "medium", "high"]
ActionState = Literal[
    "ready", "blocked", "escalated", "executing", "completed", "failed", "rejected"
]
EventKind = Literal[
    "proposed", "execution_started", "execution_rejected", "completed", "failed", "intervention"
]
OverrideCheck = Literal["risk_review", "contamination_suspected"]


class PolicyCreate(DomainModel):
    """Immutable revision; the highest revision is always used for new decisions."""

    policy_id: Identifier
    revision: int = Field(ge=1)
    sop_refs: list[SOPReferencePayload] = Field(min_length=1)
    allowed_zones: list[Identifier] = Field(min_length=1)
    require_maintenance: bool = True
    require_calibration: bool = True
    overridable_checks: list[OverrideCheck] = Field(default_factory=list)


class PolicyRead(PolicyCreate):
    instance_id: Identifier
    created_at: datetime


class ActionProposal(DomainModel):
    """Agent intent. Policy and equipment state are loaded by the server."""

    actor_id: Identifier
    task_id: Identifier
    name: Text
    target_id: Identifier
    equipment_id: Identifier
    zone_id: Identifier
    policy_id: Identifier
    reason: Text
    risk_level: RiskLevel
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    evidence: list[ActionEvidence] = Field(default_factory=list)


class ExecutionRequest(DomainModel):
    actor_id: Identifier
    reason: Text


class InterventionRequest(DomainModel):
    operator_id: Identifier
    decision_id: Identifier
    kind: Literal["override", "reject"]
    reason: Text
    evidence: list[ActionEvidence] = Field(min_length=1)


class PolicyCheck(DomainModel):
    code: Text
    decision: Decision
    reason: Text
    overridable: bool = False


class DecisionContext(DomainModel):
    policy: PolicyRead | None
    records: dict[str, OperationalRecordRead | None]


class PolicyEvaluation(DomainModel):
    """Frozen-in-history inputs and both original and effective policy decisions."""

    ruleset_version: str = "cleanroom-action-v1"
    evaluated_at: datetime
    policy_id: Identifier
    risk_level: RiskLevel
    decision: Decision
    effective_decision: Decision
    checks: list[PolicyCheck]
    context: DecisionContext
    overridden_by: Identifier | None = None


class ExecutionResult(DomainModel):
    outcome: Literal["completed", "failed"]
    summary: Text
    outputs: dict[str, JsonValue] = Field(default_factory=dict)


class IncidentRead(DomainModel):
    id: Identifier
    instance_id: Identifier
    action_id: Identifier
    task_id: Identifier
    event_id: Identifier
    created_at: datetime
    severity: Literal["medium", "high"]
    summary: Text
    requires_operator: bool = True


class InterventionRead(InterventionRequest):
    id: Identifier
    instance_id: Identifier
    action_id: Identifier
    task_id: Identifier
    event_id: Identifier
    created_at: datetime
    accepted: bool
    explanation: Text


class ActionEventRead(DomainModel):
    """One append-only provenance entry, including the exact decision context."""

    id: Identifier
    instance_id: Identifier
    action_id: Identifier
    task_id: Identifier
    sequence: int
    kind: EventKind
    timestamp: datetime
    actor_id: Identifier
    reason: Text
    state: ActionState
    proposal: ActionProposal
    evaluation: PolicyEvaluation
    result: ExecutionResult | None = None
    incident: IncidentRead | None = None
    intervention: InterventionRead | None = None


class ActionSummary(DomainModel):
    id: Identifier
    instance_id: Identifier
    created_at: datetime
    state: ActionState
    decision_id: Identifier
    proposal: ActionProposal


class ActionRead(ActionSummary):
    events: list[ActionEventRead]
