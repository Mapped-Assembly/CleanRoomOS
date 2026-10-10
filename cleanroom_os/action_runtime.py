"""Persistent action lifecycle: decide, commit intent, dispatch, append outcome."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from cleanroom_os.action_models import (
    ActionEventRead,
    ActionProposal,
    ActionRead,
    ActionSummary,
    DecisionContext,
    ExecutionRequest,
    ExecutionResult,
    IncidentRead,
    InterventionRead,
    InterventionRequest,
    PolicyCheck,
    PolicyCreate,
    PolicyEvaluation,
    PolicyRead,
)
from cleanroom_os.action_policy import context_fingerprint, evaluate, finish
from cleanroom_os.persistence import (
    Base,
    CleanroomInstanceRow,
    Database,
    DuplicateIdentifierError,
    OperationalRecordRow,
    PersistenceError,
    RuntimeRepository,
    UnknownInstanceError,
    normalize_utc,
    utc_now,
)


class PolicyRow(Base):
    __tablename__ = "action_policies"
    instance_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("cleanroom_instances.id", ondelete="RESTRICT"), primary_key=True
    )
    policy_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ActionRow(Base):
    """Mutable lifecycle cursor; the proposal and event history are never rewritten."""

    __tablename__ = "audited_actions"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    instance_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("cleanroom_instances.id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    proposal: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    authorization: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    __table_args__ = (
        UniqueConstraint("id", "instance_id"),
        Index(
            "ix_audited_actions_instance_task_created", "instance_id", "task_id", "created_at", "id"
        ),
        Index("ix_audited_actions_instance_created", "instance_id", "created_at", "id"),
    )


class ActionEventRow(Base):
    """Append-only entries; incidents and interventions are atomic parts of an event."""

    __tablename__ = "action_events"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    has_incident: Mapped[bool] = mapped_column(Boolean, nullable=False)
    has_intervention: Mapped[bool] = mapped_column(Boolean, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["action_id", "instance_id"],
            ["audited_actions.id", "audited_actions.instance_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("action_id", "sequence"),
        Index("ix_action_events_instance_task_time", "instance_id", "task_id", "timestamp", "id"),
        Index("ix_action_events_instance_time", "instance_id", "timestamp", "id"),
    )


class UnknownActionError(PersistenceError):
    """The requested action is absent from this instance."""


class ActionExecutor(Protocol):
    """Trusted adapter; action_id must be used as its external idempotency key."""

    def execute(self, action_id: str, proposal: ActionProposal) -> ExecutionResult: ...


class SimulatedExecutor:
    """Explicit simulation only: never reports physical hardware work."""

    def execute(self, action_id: str, proposal: ActionProposal) -> ExecutionResult:
        return ExecutionResult(
            outcome="completed",
            summary="Simulation completed; no equipment was actuated",
            outputs={"mode": "simulation", "action_id": action_id, "target_id": proposal.target_id},
        )


class ActionRuntime:
    def __init__(
        self,
        database: Database,
        executor: ActionExecutor | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.database, self.executor, self.clock = database, executor, clock

    @staticmethod
    def _require_instance(session: Session, instance_id: str) -> None:
        if session.get(CleanroomInstanceRow, instance_id) is None:
            raise UnknownInstanceError(f"Unknown cleanroom instance: {instance_id}")

    @staticmethod
    def _action(session: Session, instance_id: str, action_id: str) -> ActionRow:
        row = session.scalar(
            select(ActionRow).where(ActionRow.id == action_id, ActionRow.instance_id == instance_id)
        )
        if row is None:
            raise UnknownActionError("Action not found in this instance")
        return row

    @staticmethod
    def _latest_policy(session: Session, instance_id: str, policy_id: str) -> PolicyRead | None:
        row = session.scalar(
            select(PolicyRow)
            .where(
                PolicyRow.instance_id == instance_id,
                PolicyRow.policy_id == policy_id,
            )
            .order_by(PolicyRow.revision.desc())
            .limit(1)
        )
        return PolicyRead.model_validate(row.payload) if row else None

    def create_policy(self, instance_id: str, data: PolicyCreate) -> PolicyRead:
        with self.database.instance_session(instance_id) as session:
            previous = self._latest_policy(session, instance_id, data.policy_id)
            if previous and data.revision <= previous.revision:
                raise DuplicateIdentifierError(
                    "Policy revisions must increase; existing revisions are immutable"
                )
            policy = PolicyRead(
                **data.model_dump(), instance_id=instance_id, created_at=self.clock()
            )
            session.add(
                PolicyRow(
                    instance_id=instance_id,
                    policy_id=data.policy_id,
                    revision=data.revision,
                    payload=policy.model_dump(mode="json"),
                )
            )
        return policy

    def list_policies(
        self, instance_id: str, limit: int = 100, offset: int = 0
    ) -> list[PolicyRead]:
        with self.database.session() as session:
            self._require_instance(session, instance_id)
            rows = session.scalars(
                select(PolicyRow)
                .where(PolicyRow.instance_id == instance_id)
                .order_by(PolicyRow.policy_id, PolicyRow.revision)
                .limit(limit)
                .offset(offset)
            ).all()
            return [PolicyRead.model_validate(row.payload) for row in rows]

    def _context(
        self, session: Session, instance_id: str, proposal: ActionProposal
    ) -> DecisionContext:
        records = {}
        for kind, identifier in (("agent", proposal.actor_id), ("task", proposal.task_id)):
            row = session.scalar(
                select(OperationalRecordRow).where(
                    OperationalRecordRow.instance_id == instance_id,
                    OperationalRecordRow.kind == kind,
                    OperationalRecordRow.id == identifier,
                )
            )
            records["actor" if kind == "agent" else kind] = (
                RuntimeRepository._record_from_row(row) if row else None
            )
        for kind in ("maintenance", "calibration", "contamination"):
            key, value = (
                ("zone_id", proposal.zone_id)
                if kind == "contamination"
                else ("equipment_id", proposal.equipment_id)
            )
            row = session.scalar(
                select(OperationalRecordRow)
                .where(
                    OperationalRecordRow.instance_id == instance_id,
                    OperationalRecordRow.kind == kind,
                    OperationalRecordRow.payload[key].as_string() == value,
                )
                .order_by(OperationalRecordRow.created_at.desc(), OperationalRecordRow.id.desc())
                .limit(1)
            )
            records[kind] = RuntimeRepository._record_from_row(row) if row else None
        return DecisionContext(
            policy=self._latest_policy(session, instance_id, proposal.policy_id), records=records
        )

    @staticmethod
    def _state(evaluation: PolicyEvaluation) -> str:
        return {"allow": "ready", "block": "blocked", "escalate": "escalated"}[
            evaluation.effective_decision
        ]

    def _append(
        self,
        session: Session,
        row: ActionRow,
        kind: str,
        actor_id: str,
        reason: str,
        evaluation: PolicyEvaluation,
        *,
        result: ExecutionResult | None = None,
        intervention: InterventionRead | None = None,
        event_id: str | None = None,
    ) -> ActionEventRead:
        event_id, now = event_id or uuid4().hex, self.clock()
        incident = None
        if (
            kind in {"proposed", "execution_rejected"} and evaluation.effective_decision != "allow"
        ) or kind == "failed":
            reasons = [check.reason for check in evaluation.checks if check.decision != "allow"]
            incident = IncidentRead(
                id=uuid4().hex,
                instance_id=row.instance_id,
                action_id=row.id,
                task_id=row.task_id,
                event_id=event_id,
                created_at=now,
                severity="high"
                if evaluation.effective_decision == "block" or kind == "failed"
                else "medium",
                summary=result.summary if result else "; ".join(reasons),
            )
        row.sequence += 1
        entry = ActionEventRead(
            id=event_id,
            instance_id=row.instance_id,
            action_id=row.id,
            task_id=row.task_id,
            sequence=row.sequence,
            kind=kind,
            timestamp=now,
            actor_id=actor_id,
            reason=reason,
            state=row.state,
            proposal=ActionProposal.model_validate(row.proposal),
            evaluation=evaluation,
            result=result,
            incident=incident,
            intervention=intervention,
        )
        session.add(
            ActionEventRow(
                id=entry.id,
                instance_id=row.instance_id,
                action_id=row.id,
                task_id=row.task_id,
                sequence=entry.sequence,
                kind=kind,
                timestamp=now,
                has_incident=incident is not None,
                has_intervention=intervention is not None,
                payload=entry.model_dump(mode="json"),
            )
        )
        session.flush()
        return entry

    def propose(self, instance_id: str, proposal: ActionProposal) -> ActionRead:
        with self.database.instance_session(instance_id) as session:
            now = self.clock()
            evaluation = evaluate(proposal, self._context(session, instance_id, proposal), now)
            row = ActionRow(
                id=uuid4().hex,
                instance_id=instance_id,
                task_id=proposal.task_id,
                actor_id=proposal.actor_id,
                created_at=now,
                state=self._state(evaluation),
                proposal=proposal.model_dump(mode="json"),
                sequence=0,
            )
            session.add(row)
            session.flush()
            entry = self._append(
                session, row, "proposed", proposal.actor_id, proposal.reason, evaluation
            )
            row.decision_id = entry.id
            action_id = row.id
        return self.get_action(instance_id, action_id)

    def execute(self, instance_id: str, action_id: str, request: ExecutionRequest) -> ActionRead:
        """Only a committed execution_started event can cause an adapter invocation."""
        dispatch = False
        with self.database.instance_session(instance_id) as session:
            row = self._action(session, instance_id, action_id)
            proposal = ActionProposal.model_validate(row.proposal)
            evaluation = evaluate(
                proposal, self._context(session, instance_id, proposal), self.clock()
            )
            eligible = row.state == "ready"
            if not eligible:
                evaluation.checks.append(
                    PolicyCheck(
                        code="lifecycle", decision="block", reason=f"Action is already {row.state}"
                    )
                )
                finish(evaluation)
            elif request.actor_id != proposal.actor_id:
                evaluation.checks.append(
                    PolicyCheck(
                        code="execution_actor",
                        decision="block",
                        reason="Execution actor differs from the proposal",
                    )
                )
                finish(evaluation)
            else:
                if row.authorization and row.authorization["fingerprint"] == context_fingerprint(
                    evaluation
                ):
                    evaluation.effective_decision = "allow"
                    evaluation.overridden_by = row.authorization["intervention_id"]
                if self.executor is None:
                    evaluation.checks.append(
                        PolicyCheck(
                            code="executor_missing",
                            decision="block",
                            reason="No execution adapter is configured",
                        )
                    )
                    finish(evaluation)
            if eligible:
                row.authorization = None
                row.state = (
                    "executing"
                    if evaluation.effective_decision == "allow"
                    else self._state(evaluation)
                )
            dispatch = eligible and evaluation.effective_decision == "allow"
            entry = self._append(
                session,
                row,
                "execution_started" if dispatch else "execution_rejected",
                request.actor_id,
                request.reason,
                evaluation,
            )
            if eligible:
                row.decision_id = entry.id
        if dispatch:
            # No DB locks are held over adapter/network calls. A crash leaves 'executing'
            # durable and non-retryable, rather than risking a duplicate physical effect.
            try:
                assert self.executor is not None
                result = ExecutionResult.model_validate(self.executor.execute(action_id, proposal))
            except Exception:
                result = ExecutionResult(
                    outcome="failed",
                    summary="Execution adapter failed; outcome requires operator inspection",
                )
            with self.database.instance_session(instance_id) as session:
                row = self._action(session, instance_id, action_id)
                row.state = result.outcome
                self._append(
                    session,
                    row,
                    result.outcome,
                    proposal.actor_id,
                    request.reason,
                    evaluation,
                    result=result,
                )
        return self.get_action(instance_id, action_id)

    def intervene(
        self, instance_id: str, action_id: str, request: InterventionRequest
    ) -> ActionRead:
        with self.database.instance_session(instance_id) as session:
            row = self._action(session, instance_id, action_id)
            now = self.clock()
            proposal = ActionProposal.model_validate(row.proposal)
            evaluation = evaluate(proposal, self._context(session, instance_id, proposal), now)
            previous = ActionEventRead.model_validate(
                session.get(ActionEventRow, row.decision_id).payload
            )
            unchanged = context_fingerprint(previous.evaluation) == context_fingerprint(evaluation)
            pending = [check for check in evaluation.checks if check.decision != "allow"]
            accepted, explanation = False, "Action is not awaiting a decision"
            mutable = row.state in {"ready", "blocked", "escalated"}
            if mutable:
                if request.decision_id != row.decision_id:
                    explanation = "Review the current decision; the supplied decision ID is stale"
                elif any(item.captured_at > now for item in request.evidence):
                    explanation = "Operator evidence cannot be future-dated"
                elif request.kind == "reject":
                    accepted, explanation = True, "Operator rejected the action"
                    row.state, row.authorization = "rejected", None
                    evaluation.effective_decision = "block"
                elif not unchanged:
                    explanation = "Policy or evidence changed; review the newly evaluated decision"
                elif (
                    row.state != "escalated"
                    or not pending
                    or not all(check.overridable for check in pending)
                ):
                    explanation = "Only explicitly overridable escalations may be overridden; hard blocks cannot"
                else:
                    accepted, explanation = (
                        True,
                        "Operator approved the policy-permitted escalation",
                    )
            event_id, intervention_id = uuid4().hex, uuid4().hex
            if accepted and request.kind == "override":
                row.state = "ready"
                row.authorization = {
                    "fingerprint": context_fingerprint(evaluation),
                    "intervention_id": intervention_id,
                }
                evaluation.effective_decision, evaluation.overridden_by = "allow", intervention_id
            elif mutable and not accepted and not unchanged:
                row.state, row.authorization = self._state(evaluation), None
            intervention = InterventionRead(
                **request.model_dump(),
                id=intervention_id,
                instance_id=instance_id,
                action_id=action_id,
                task_id=row.task_id,
                event_id=event_id,
                created_at=now,
                accepted=accepted,
                explanation=explanation,
            )
            entry = self._append(
                session,
                row,
                "intervention",
                request.operator_id,
                request.reason,
                evaluation,
                intervention=intervention,
                event_id=event_id,
            )
            if accepted or (mutable and not unchanged):
                row.decision_id = entry.id
        return self.get_action(instance_id, action_id)

    @staticmethod
    def _summary(row: ActionRow) -> ActionSummary:
        return ActionSummary(
            id=row.id,
            instance_id=row.instance_id,
            created_at=normalize_utc(row.created_at),
            state=row.state,
            decision_id=row.decision_id,
            proposal=ActionProposal.model_validate(row.proposal),
        )

    def get_action(self, instance_id: str, action_id: str) -> ActionRead:
        with self.database.instance_session(instance_id) as session:
            row = self._action(session, instance_id, action_id)
            events = session.scalars(
                select(ActionEventRow)
                .where(
                    ActionEventRow.instance_id == instance_id,
                    ActionEventRow.action_id == action_id,
                )
                .order_by(ActionEventRow.sequence)
            ).all()
            return ActionRead(
                **self._summary(row).model_dump(),
                events=[ActionEventRead.model_validate(event.payload) for event in events],
            )

    def list_actions(
        self, instance_id: str, task_id: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[ActionSummary]:
        with self.database.session() as session:
            self._require_instance(session, instance_id)
            statement = select(ActionRow).where(ActionRow.instance_id == instance_id)
            if task_id is not None:
                statement = statement.where(ActionRow.task_id == task_id)
            rows = session.scalars(
                statement.order_by(ActionRow.created_at, ActionRow.id).limit(limit).offset(offset)
            ).all()
            return [self._summary(row) for row in rows]

    def history(
        self,
        instance_id: str,
        *,
        action_id: str | None = None,
        task_id: str | None = None,
        category: str = "events",
        limit: int = 100,
        offset: int = 0,
    ) -> list[ActionEventRead]:
        with self.database.session() as session:
            self._require_instance(session, instance_id)
            statement = select(ActionEventRow).where(ActionEventRow.instance_id == instance_id)
            if action_id is not None:
                statement = statement.where(ActionEventRow.action_id == action_id)
            if task_id is not None:
                statement = statement.where(ActionEventRow.task_id == task_id)
            if category == "incidents":
                statement = statement.where(ActionEventRow.has_incident.is_(True))
            elif category == "interventions":
                statement = statement.where(ActionEventRow.has_intervention.is_(True))
            elif category == "decisions":
                statement = statement.where(ActionEventRow.kind.not_in(["completed", "failed"]))
            rows = session.scalars(
                statement.order_by(
                    ActionEventRow.timestamp, ActionEventRow.action_id, ActionEventRow.sequence
                )
                .limit(limit)
                .offset(offset)
            ).all()
            return [ActionEventRead.model_validate(row.payload) for row in rows]
