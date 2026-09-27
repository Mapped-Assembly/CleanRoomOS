"""Validated exchange contracts; these models do not execute or approve workflows."""
from __future__ import annotations

from typing import Annotated, Literal, Self, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints,
    ValidationError, field_validator, model_validator,
)

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Count = Annotated[int, Field(strict=True, gt=0)]
Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]
SourceKind = Literal["sop", "recipe", "schedule", "lims", "human"]
Role = Literal["manufacturing", "qa"]
State = Literal["context_pending", "validated", "plan_proposed", "blocked", "plan_approved", "collection_simulated", "results_received", "review_ready", "qa_approved", "qa_rejected"]
Unit = Literal["CFU", "CFU/m3", "CFU/plate", "CFU/cm2", "particles/m3"]


class Contract(BaseModel):
    """Reject unknown fields and validate subsequent assignment."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class SourceReference(Contract):
    """Reference a versioned document section or a versioned fixture record."""

    kind: SourceKind
    source_id: Identifier
    version: Text
    locator: Text


Evidence = Annotated[list[SourceReference], Field(min_length=1)]


class Unknown(Contract):
    """An explicit information gap, never an implicit default or pass."""

    status: Literal["unknown"]
    reason: Text
    sources: Evidence


class Threshold(Contract):
    """An authoritative bound with an explicit comparison and measurement unit."""

    value: Number
    unit: Unit
    operator: Literal["lt", "le", "gt", "ge", "eq"]
    sources: Evidence

    @model_validator(mode="after")
    def require_authority(self) -> Self:
        """Reject limits that originate only from derived or operational data."""
        if not any(s.kind in {"sop", "recipe"} for s in self.sources):
            raise ValueError("Threshold requires SOP or recipe evidence")
        return self


class SamplingRequirement(Contract):
    """Required sample obligations preserved independently of scheduling."""

    requirement_id: Identifier
    room_id: Identifier
    sample_type: Text
    count: Count | Unknown
    threshold: Threshold | Unknown
    sources: Evidence

    @model_validator(mode="after")
    def require_authority(self) -> Self:
        """Require a procedure or recipe source for each obligation."""
        if not any(s.kind in {"sop", "recipe"} for s in self.sources):
            raise ValueError("Requirement requires SOP or recipe evidence")
        return self


class SOPRequirements(Contract):
    """Versioned requirements extracted from the facility SOP."""

    document_id: Identifier
    version: Text
    requirements: Annotated[list[SamplingRequirement], Field(min_length=1)]

    @model_validator(mode="after")
    def check_sources(self) -> Self:
        """Bind extracted requirements to this exact source version."""
        unique([r.requirement_id for r in self.requirements], "requirement")
        pairs = [(r.room_id, r.sample_type) for r in self.requirements]
        if len(set(pairs)) != len(pairs):
            raise ValueError("Duplicate room/sample-type obligations in source")
        for r in self.requirements:
            if not any(s.kind == "sop" and s.source_id == self.document_id and s.version == self.version for s in r.sources):
                raise ValueError("SOP requirement lacks matching document/version evidence")
        return self


class Recipe(Contract):
    """Authoritative versioned execution requirements."""

    recipe_id: Identifier
    version: Text
    requirements: Annotated[list[SamplingRequirement], Field(min_length=1)]

    @model_validator(mode="after")
    def check_sources(self) -> Self:
        """Bind recipe obligations to this exact source version."""
        unique([r.requirement_id for r in self.requirements], "requirement")
        pairs = [(r.room_id, r.sample_type) for r in self.requirements]
        if len(set(pairs)) != len(pairs):
            raise ValueError("Duplicate room/sample-type obligations in source")
        for r in self.requirements:
            if not any(s.kind == "recipe" and s.source_id == self.recipe_id and s.version == self.version for s in r.sources):
                raise ValueError("Recipe requirement lacks matching recipe/version evidence")
        return self


class TimeWindow(Contract):
    """An ordered interval with aware endpoints and an IANA time zone."""

    start: AwareDatetime
    end: AwareDatetime
    timezone: Text

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value: str) -> str:
        """Reject nonexistent IANA time zones."""
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Unknown IANA time zone") from exc
        return value

    @model_validator(mode="after")
    def ordered(self) -> Self:
        """Reject inverted intervals and offsets inconsistent with the named zone."""
        if self.end <= self.start:
            raise ValueError("Window end must follow start")
        zone = ZoneInfo(self.timezone)
        if any(t.utcoffset() != t.astimezone(zone).utcoffset() for t in (self.start, self.end)):
            raise ValueError("Timestamp offset does not match time zone")
        return self


class Room(Contract):
    """A source-identified facility room."""

    room_id: Identifier
    name: Text
    sources: Evidence


class Occupancy(Contract):
    """Workers occupying a room during a sourced interval."""

    room_id: Identifier
    worker_ids: Annotated[list[Identifier], Field(min_length=1)]
    window: TimeWindow
    sources: Evidence


class Availability(Contract):
    """Explicit known access windows; an empty list means no known availability."""

    room_id: Identifier
    windows: list[TimeWindow]
    sources: Evidence


class DailySchedule(Contract):
    """Versioned operational constraints, never an authority to change sampling."""

    schedule_id: Identifier
    version: Text
    shift: TimeWindow
    occupancy: list[Occupancy]
    availability: list[Availability]

    @model_validator(mode="after")
    def check_sources(self) -> Self:
        """Bind each constraint to the schedule version."""
        unique([a.room_id for a in self.availability], "availability room")
        for entry in [*self.occupancy, *self.availability]:
            if not any(s.kind == "schedule" and s.source_id == self.schedule_id and s.version == self.version for s in entry.sources):
                raise ValueError("Constraint lacks matching schedule/version evidence")
        return self


class PlanningConflict(Contract):
    """An unresolved obstacle retaining the affected requirement identity."""

    conflict_id: Identifier
    code: Text = "unspecified"
    requirement_id: Identifier
    room_id: Identifier
    reason: Text
    sources: Evidence


class PlannedSample(Contract):
    """A derived sample with a requirement reference and scheduling evidence."""

    sample_id: Identifier
    requirement_id: Identifier
    room_id: Identifier
    sample_type: Text
    window: TimeWindow
    sequencing_reason: Text
    sources: Evidence


class SamplingPlan(Contract):
    """A versioned derived proposal including all required and blocked obligations."""

    plan_id: Identifier
    revision: Count
    requirements: Annotated[list[SamplingRequirement], Field(min_length=1)]
    samples: list[PlannedSample]
    conflicts: list[PlanningConflict]

    @model_validator(mode="after")
    def preserve_obligations(self) -> Self:
        """Reject orphan samples, altered identities, and silently dropped counts."""
        unique([r.requirement_id for r in self.requirements], "requirement")
        unique([s.sample_id for s in self.samples], "sample")
        unique([c.conflict_id for c in self.conflicts], "conflict")
        requirements = {r.requirement_id: r for r in self.requirements}
        for entry in [*self.samples, *self.conflicts]:
            r = requirements.get(entry.requirement_id)
            if r is None or entry.room_id != r.room_id:
                raise ValueError("Unknown requirement or mismatched room")
            if isinstance(entry, PlannedSample):
                if entry.sample_type != r.sample_type:
                    raise ValueError("Sample type differs from requirement")
                if not all(source in entry.sources for source in r.sources):
                    raise ValueError("Sample must retain requirement source references")
                if not any(s.kind == "schedule" for s in entry.sources):
                    raise ValueError("Planned sample requires scheduling evidence")
        for r in self.requirements:
            count = sum(s.requirement_id == r.requirement_id for s in self.samples)
            blocked = any(c.requirement_id == r.requirement_id for c in self.conflicts)
            if isinstance(r.count, Unknown):
                if count or not blocked:
                    raise ValueError("Unknown count requires a conflict and cannot be scheduled")
            elif count > r.count or (count != r.count and not blocked):
                raise ValueError("Required count changed or missing without conflict")
        return self


class LIMSResult(Contract):
    """A source result; matching to a plan occurs in the evaluator, not ingestion."""

    result_id: Identifier
    plan_id: Identifier
    plan_revision: Count
    sample_id: Identifier
    room_id: Identifier
    sample_type: Text
    collected_at: AwareDatetime
    value: Number | Unknown
    unit: Unit
    sources: Evidence

    @model_validator(mode="after")
    def lims_authority(self) -> Self:
        """Require the system of record for every received result."""
        if not any(s.kind == "lims" for s in self.sources):
            raise ValueError("Result requires LIMS source evidence")
        return self


class QAFinding(Contract):
    """An evidence-backed review item, not a final compliance decision."""

    finding_id: Identifier
    code: Text = "unspecified"
    requirement_id: Identifier | None = None
    kind: Literal["missing", "unmatched", "duplicate", "mismatch", "out_of_limit", "unknown", "blocked"]
    sample_id: Identifier | None
    result_ids: list[Identifier]
    description: Text
    sources: Evidence


class ResultBatch(Contract):
    """A complete LIMS import snapshot; distinct results may share a sample ID."""

    results: list[LIMSResult]

    @model_validator(mode="after")
    def unique_result_ids(self) -> Self:
        """Reject ambiguous record identities without dropping evidence."""
        unique([r.result_id for r in self.results], "result")
        return self


NonnegativeCount = Annotated[int, Field(strict=True, ge=0)]


class EvaluationCounts(Contract):
    """Separate sample obligations from received-record classifications."""

    known_required: NonnegativeCount
    expected: NonnegativeCount
    received: NonnegativeCount
    matched: NonnegativeCount
    missing: NonnegativeCount
    mismatched_samples: NonnegativeCount
    blocked: NonnegativeCount
    unknown_count_requirements: NonnegativeCount
    matched_records: NonnegativeCount
    mismatched_records: NonnegativeCount
    unmatched_records: NonnegativeCount
    duplicate_records: NonnegativeCount

    @model_validator(mode="after")
    def reconcile(self) -> Self:
        """Require complete sample and result partitions; duplicates are an overlay."""
        if self.known_required != self.expected + self.blocked:
            raise ValueError("Known required count must equal expected plus blocked")
        if self.expected != self.matched + self.missing + self.mismatched_samples:
            raise ValueError("Expected samples must reconcile with matched, missing and mismatched samples")
        if self.received != self.matched_records + self.mismatched_records + self.unmatched_records:
            raise ValueError("Received records must reconcile with result classifications")
        if self.duplicate_records > self.received:
            raise ValueError("Duplicate record count exceeds received count")
        return self


class ResultEvaluation(Contract):
    """Deterministic evidence for one exact plan revision, never a QA decision."""

    plan_id: Identifier
    plan_revision: Count
    counts: EvaluationCounts
    findings: list[QAFinding]
    sources: Evidence


class QAReviewPackage(Contract):
    """A versioned package of a plan, results, and traceable findings."""

    package_id: Identifier
    revision: Count
    plan: SamplingPlan
    results: list[LIMSResult]
    findings: list[QAFinding]
    counts: EvaluationCounts | None = None
    context: PlanningContext | None = None
    prior_decisions: list[HumanPlanningDecision | HumanQADecision] = Field(default_factory=list)
    collection_status: Literal["not_collected", "simulated"] = "not_collected"
    completeness: Literal["incomplete", "complete"] = "incomplete"
    summary: Text | None = None
    required_actions: list[Text] = Field(default_factory=list)
    sources: Evidence

    @model_validator(mode="after")
    def unique_records(self) -> Self:
        """Keep record IDs unambiguous while retaining mismatched LIMS evidence."""
        unique([r.result_id for r in self.results], "result")
        unique([f.finding_id for f in self.findings], "finding")
        result_ids = {r.result_id for r in self.results}
        if any(not set(f.result_ids) <= result_ids for f in self.findings):
            raise ValueError("Finding references absent result records")
        return self


class HumanPlanningDecision(Contract):
    """Operational resolution for an exact plan revision, never a QA release."""

    decision_id: Identifier
    plan_id: Identifier
    plan_revision: Count
    actor_id: Identifier
    role: Role
    decision: Literal["allow", "disallow", "request_resolution"]
    rationale: Text
    decided_at: AwareDatetime
    sources: Evidence


class HumanQADecision(Contract):
    """An attributed final QA decision for exact package and plan revisions."""

    decision_id: Identifier
    package_id: Identifier
    package_revision: Count
    plan_id: Identifier
    plan_revision: Count
    actor_id: Identifier
    role: Literal["qa"]
    decision: Literal["approve", "reject", "request_resolution"]
    rationale: Text
    decided_at: AwareDatetime
    sources: Evidence


class WorkflowTransition(Contract):
    """A requested transition; the future controller must authorize and persist it."""

    event_id: Identifier
    expected_state: State
    requested_state: State
    plan_id: Identifier
    plan_revision: Count
    actor_id: Identifier
    actor_role: Literal["controller", "manufacturing", "qa"]
    decision_id: Identifier | None
    occurred_at: AwareDatetime
    sources: Evidence


class ExecutionAssumptions(Contract):
    """Explicit scenario timing and resource inputs, not planner defaults."""

    technician_id: Identifier
    technician_availability: TimeWindow | Unknown
    starting_room_id: Identifier
    sample_minutes: Count | Unknown
    setup_minutes_per_room: Annotated[int, Field(strict=True, ge=0)] | Unknown
    travel_minutes_between_rooms: Annotated[int, Field(strict=True, ge=0)] | Unknown
    collection_mode: Literal["sequential"]
    sources: Evidence


class PlanningContext(Contract):
    """Validated authoritative inputs with no silent source precedence override."""

    sop: SOPRequirements
    recipe: Recipe
    rooms: Annotated[list[Room], Field(min_length=1)]
    schedule: DailySchedule
    execution: ExecutionAssumptions | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        """Reject unknown rooms and contradictory obligations for the same sample type."""
        unique([r.room_id for r in self.rooms], "room")
        rooms = {r.room_id for r in self.rooms}
        if self.execution and self.execution.starting_room_id not in rooms:
            raise ValueError("Execution starts in an unknown room")
        seen: dict[tuple[str, str], SamplingRequirement] = {}
        for r in [*self.sop.requirements, *self.recipe.requirements]:
            if r.room_id not in rooms:
                raise ValueError("Requirement references unknown room")
            key = (r.room_id, r.sample_type)
            previous = seen.get(key)
            if previous:
                old_limit = previous.threshold.model_dump(exclude={"sources"})
                new_limit = r.threshold.model_dump(exclude={"sources"})
                if previous.count != r.count or old_limit != new_limit:
                    raise ValueError("Conflicting authoritative requirements; human resolution required")
            seen[key] = r
        if any(e.room_id not in rooms for e in [*self.schedule.occupancy, *self.schedule.availability]):
            raise ValueError("Schedule references unknown room")
        return self


class ValidationIssue(Contract):
    """A machine-readable validation failure without leaking the input payload."""

    path: list[str | int]
    code: str
    message: str


class ContractValidationError(ValueError):
    """Expose structured errors to controllers and agent adapters."""

    def __init__(self, issues: list[ValidationIssue]) -> None:
        """Retain typed issues and provide a readable summary."""
        self.issues = issues
        super().__init__("; ".join(i.message for i in issues))


T = TypeVar("T", bound=Contract)


def parse_contract(model: type[T], payload: str) -> T:
    """Parse JSON-only exchange data and normalize all Pydantic failures."""
    try:
        return model.model_validate_json(payload)
    except ValidationError as exc:
        raise ContractValidationError([
            ValidationIssue(path=list(e["loc"]), code=e["type"], message=e["msg"])
            for e in exc.errors(include_input=False, include_url=False)
        ]) from exc


def unique(values: list[str], name: str) -> None:
    """Reject duplicate identifiers within one collection."""
    if len(set(values)) != len(values):
        raise ValueError(f"Duplicate {name} identifiers")
