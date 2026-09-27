"""Deterministic single-technician planning and producer-independent validation."""
from datetime import timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

from cleanroom_os.contracts import (
    ContractValidationError, PlannedSample, PlanningConflict, PlanningContext,
    SamplingPlan, SourceReference, TimeWindow, Unknown, ValidationIssue, parse_contract,
)


def _evidence(context, requirement):
    sources = list(requirement.sources)
    for entry in [*context.schedule.availability, *context.schedule.occupancy]:
        if entry.room_id == requirement.room_id:
            sources.extend(entry.sources)
    if context.execution:
        sources.extend(context.execution.sources)
    sources.append(SourceReference(kind='schedule', source_id=context.schedule.schedule_id,
                                   version=context.schedule.version, locator='/data/shift'))
    return [s for i, s in enumerate(sources) if s not in sources[:i]]


def _execution_known(context):
    e = context.execution
    return e is not None and not any(isinstance(v, Unknown) for v in (
        e.sample_minutes, e.setup_minutes_per_room, e.travel_minutes_between_rooms,
        e.technician_availability))


def sample_id(context, requirement, index):
    """Bind identity to recipe, requirement and ordinal, independent of schedule/revision."""
    key = f'{context.recipe.recipe_id}\0{requirement.requirement_id}\0{index}'
    return 'SAMPLE-' + sha256(key.encode()).hexdigest()[:32]


def _free_windows(context, room):
    """Intersect access with shift/resource bounds and subtract occupied intervals."""
    e = context.execution
    segments = []
    for access in context.schedule.availability:
        if access.room_id != room:
            continue
        for w in access.windows:
            start = max(w.start, context.schedule.shift.start, e.technician_availability.start)
            end = min(w.end, context.schedule.shift.end, e.technician_availability.end)
            parts = [(start, end)] if start < end else []
            for o in context.schedule.occupancy:
                if o.room_id != room:
                    continue
                remaining = []
                for a, b in parts:
                    if o.window.end <= a or o.window.start >= b:
                        remaining.append((a, b))
                    else:
                        if a < o.window.start:
                            remaining.append((a, o.window.start))
                        if o.window.end < b:
                            remaining.append((o.window.end, b))
                parts = remaining
            segments.extend(parts)
    return sorted(set(segments))


def generate_plan(context: PlanningContext, *, plan_id='SAMPLING-PLAN', revision=1) -> SamplingPlan:
    """Greedily place complete requirement batches by earliest feasible start, then deadline.

    This conservative baseline does not claim global optimality. An unscheduled batch
    stays an obligation with a conflict; it is never waived or partially invented.
    """
    context = parse_contract(PlanningContext, context.model_dump_json())
    pending = list(context.recipe.requirements)
    samples, conflicts = [], []

    def block(r, code, reason):
        conflicts.append(PlanningConflict(
            conflict_id='CONFLICT-' + sha256(f'{r.requirement_id}:{code}'.encode()).hexdigest()[:32],
            requirement_id=r.requirement_id, room_id=r.room_id, code=code,
            reason=f'{code}: {reason}', sources=_evidence(context, r)))

    for r in pending[:]:
        access = next((a for a in context.schedule.availability if a.room_id == r.room_id), None)
        reasons = []
        if isinstance(r.count, Unknown):
            reasons.append(('unknown_count', r.count.reason))
        if isinstance(r.threshold, Unknown):
            reasons.append(('unknown_threshold', r.threshold.reason))
        if not _execution_known(context):
            reasons.append(('missing_execution', 'Explicit duration, setup, travel and technician availability are required.'))
        if access is None:
            reasons.append(('missing_availability', 'No room availability information supplied.'))
        elif not access.windows:
            reasons.append(('unavailable_room', 'No known room availability; required samples remain blocked.'))
        for code, reason in reasons:
            block(r, code, reason)
        if reasons:
            pending.remove(r)
    if pending:
        e = context.execution
        cursor = max(context.schedule.shift.start, e.technician_availability.start)
        room = e.starting_room_id
        while pending:
            candidates = []
            for r in pending:
                travel = timedelta(minutes=e.travel_minutes_between_rooms if room != r.room_id else 0)
                length = timedelta(minutes=e.setup_minutes_per_room + r.count * e.sample_minutes)
                for a, b in _free_windows(context, r.room_id):
                    start = max(a, cursor + travel)
                    if start + length <= b:
                        candidates.append((start, b, r.requirement_id, r))
                        break
            if not candidates:
                for r in pending:
                    block(r, 'insufficient_window', 'No remaining window fits setup, full sample count, travel, occupancy and technician/shift bounds in this sequence; human scheduling input required.')
                break
            start, deadline, _, r = min(candidates, key=lambda c: c[:3])
            cursor = start + timedelta(minutes=e.setup_minutes_per_room)
            for index in range(1, r.count + 1):
                end = cursor + timedelta(minutes=e.sample_minutes)
                samples.append(PlannedSample(sample_id=sample_id(context, r, index),
                    requirement_id=r.requirement_id, room_id=r.room_id, sample_type=r.sample_type,
                    window=TimeWindow(start=cursor.astimezone(ZoneInfo(context.schedule.shift.timezone)),
                                      end=end.astimezone(ZoneInfo(context.schedule.shift.timezone)),
                                      timezone=context.schedule.shift.timezone),
                    sequencing_reason=f'Earliest feasible complete batch; ties use window deadline then requirement ID. Setup begins {start.isoformat()}; window ends {deadline.isoformat()}. Explicit setup, travel and single-technician constraints retained.',
                    sources=_evidence(context, r)))
                cursor = end
            room = r.room_id
            pending.remove(r)
    plan = SamplingPlan(plan_id=plan_id, revision=revision,
                        requirements=context.recipe.requirements, samples=samples, conflicts=conflicts)
    validate_plan(context, plan)
    return plan


def validate_plan(context: PlanningContext, plan: SamplingPlan) -> None:
    """Validate proposals without calling or comparing against the generating algorithm."""
    context = parse_contract(PlanningContext, context.model_dump_json())
    plan = parse_contract(SamplingPlan, plan.model_dump_json())
    issues = []

    def fail(code, message, path):
        issues.append(ValidationIssue(code=code, message=message, path=path))

    if plan.requirements != context.recipe.requirements:
        fail('changed_obligations', 'Plan must preserve exact authoritative recipe obligations', ['requirements'])
    for r in context.sop.requirements:
        if not any((q.room_id, q.sample_type, q.count) == (r.room_id, r.sample_type, r.count)
                   and q.threshold.model_dump(exclude={'sources'}) == r.threshold.model_dump(exclude={'sources'})
                   for q in plan.requirements):
            fail('missing_sop_obligation', 'SOP obligation missing from recipe/plan', ['requirements'])
    for r in plan.requirements:
        if isinstance(r.threshold, Unknown) and not any(c.requirement_id == r.requirement_id for c in plan.conflicts):
            fail('unknown_threshold', 'Unknown threshold requires a conflict', ['conflicts'])
    if plan.samples and not _execution_known(context):
        fail('missing_execution', 'Cannot schedule without explicit execution information', ['samples'])
    elif plan.samples:
        e = context.execution
        previous = None
        for i, sample in enumerate(plan.samples):
            w = sample.window
            path = ['samples', i]
            new_visit = previous is None or previous.room_id != sample.room_id or previous.window.end != w.start
            setup = timedelta(minutes=e.setup_minutes_per_room if new_visit else 0)
            visit_start = w.start - setup
            origin = previous.room_id if previous else e.starting_room_id
            travel = timedelta(minutes=e.travel_minutes_between_rooms if origin != sample.room_id else 0)
            earliest = previous.window.end if previous else max(context.schedule.shift.start, e.technician_availability.start)
            if visit_start < earliest + travel:
                fail('resource_overlap', 'Overlapping/unordered visits or insufficient setup/travel time', path)
            if w.end - w.start != timedelta(minutes=e.sample_minutes):
                fail('duration_mismatch', 'Sample duration differs from supplied duration', path)
            for bound in (context.schedule.shift, e.technician_availability):
                if not bound.start <= visit_start < w.end <= bound.end:
                    fail('outside_resource_bounds', 'Visit including setup exceeds shift or technician availability', path)
            access = next((a for a in context.schedule.availability if a.room_id == sample.room_id), None)
            if access is None or not any(a.start <= visit_start < w.end <= a.end for a in access.windows):
                fail('inaccessible_visit', 'Visit including setup is outside known room availability', path)
            elif not all(s in sample.sources for s in access.sources):
                fail('stale_schedule_evidence', 'Sample must cite current schedule evidence', path)
            if not all(s in sample.sources for s in e.sources):
                fail('missing_execution_evidence', 'Sample must cite supplied execution constraints', path)
            if any(o.room_id == sample.room_id and visit_start < o.window.end and o.window.start < w.end for o in context.schedule.occupancy):
                fail('occupied_visit', 'Visit including setup overlaps room occupancy', path)
            previous = sample
    if issues:
        raise ContractValidationError(issues)


class DeterministicPlanner:
    """JSON proposal-service adapter with explicit revision supplied by its caller."""

    def __init__(self, revision=1, plan_id='SAMPLING-PLAN'):
        self.revision = revision
        self.plan_id = plan_id

    def propose(self, context_json: str) -> str:
        return generate_plan(parse_contract(PlanningContext, context_json),
                             plan_id=self.plan_id, revision=self.revision).model_dump_json()
