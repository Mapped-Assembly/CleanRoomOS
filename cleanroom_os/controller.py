"""Transactional local workflow gates; agent proposals never authorize transitions."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Callable, Literal, Protocol, TypeVar

from pydantic import Field

from cleanroom_os.contracts import (
    Contract, HumanPlanningDecision, HumanQADecision, LIMSResult, PlanningContext,
    QAReviewPackage, ResultBatch, SamplingPlan, State, Unknown, parse_contract,
)


from cleanroom_os.planning import validate_plan
from cleanroom_os.agent_contracts import AgentResponse, RequirementsDraft, source_gaps, validate_response
from cleanroom_os.agents import OfflineRequirements
from cleanroom_os.evaluation import evaluate_results


class Snapshot(Contract):
    """Persisted run state, with prior evidence retained in the event ledger."""

    state: State = "context_pending"
    context: PlanningContext | None = None
    requirements_assessment: AgentResponse[RequirementsDraft] | None = None
    plan: SamplingPlan | None = None
    results: list[LIMSResult] = Field(default_factory=list)
    package: QAReviewPackage | None = None
    last_revision: int = 0
    last_package_revision: int = 0
    decision_ids: list[str] = Field(default_factory=list)
    resolution_required: bool = False


class RequirementsService(Protocol):
    """Extract cited requirements without changing authoritative source documents."""

    def extract(self, context_json: str) -> str:
        ...


class ProposalService(Protocol):
    """Later model adapters return JSON only and receive no controller handle."""

    def propose(self, context_json: str) -> str:
        """Return a SamplingPlan JSON proposal for supplied validated context."""
        ...


class ReviewService(Protocol):
    """Review adapters propose evidence packages without deciding QA outcomes."""

    def review(self, plan_json: str, results_json: str) -> str:
        """Return QAReviewPackage JSON for the exact supplied evidence."""
        ...


class WorkflowError(ValueError):
    """A visible blocked operation, recorded without advancing the workflow."""


R = TypeVar("R")


class Controller:
    """Own one run per SQLite file; serialize decisions and snapshots atomically."""

    def __init__(self, database: Path) -> None:
        """Initialize durable local storage without resetting an existing run."""
        self.database = database
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL, details TEXT NOT NULL, snapshot TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO snapshot VALUES (1, ?)', (Snapshot().model_dump_json(),))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open, commit or roll back, and close a bounded-wait connection."""
        db = sqlite3.connect(self.database, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def snapshot(self) -> Snapshot:
        """Return an isolated validated snapshot, not mutable controller state."""
        with self._connect() as db:
            return parse_contract(Snapshot, db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()[0])

    def events(self) -> list[dict[str, object]]:
        """Return ordered, persisted events including failed operations."""
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM events ORDER BY id')]

    def _action(self, action: str, actor: str, role: str, details: str,
                operation: Callable[[Snapshot], None], *, invalidate_on_error: bool = False) -> Snapshot:
        """Atomically gate, persist, and log an action; raise only after error commit."""
        error: Exception | None = None
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            original = parse_contract(Snapshot, db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()[0])
            current = original.model_copy(deep=True)
            try:
                if not actor.strip():
                    raise WorkflowError('Actor identity is required (demo identity is unauthenticated)')
                operation(current)
                current = parse_contract(Snapshot, current.model_dump_json())
            except Exception as exc:
                error = exc
                current = original
                if invalidate_on_error:
                    self._clear(current)
                    current.context = None
                    current.state = 'context_pending'
                    current.resolution_required = True
                details = json.dumps({'request': details, 'error_type': type(exc).__name__, 'error': str(exc)})
            db.execute('UPDATE snapshot SET payload=? WHERE id=1', (current.model_dump_json(),))
            db.execute('INSERT INTO events(at,actor,role,action,status,details,snapshot) VALUES (?,?,?,?,?,?,?)',
                       (datetime.now(timezone.utc).isoformat(), actor, role, action, 'error' if error else 'ok', details, current.model_dump_json()))
        if error:
            raise WorkflowError(f'{action} failed: {error}') from error
        return current

    @staticmethod
    def _clear(s: Snapshot) -> None:
        """Invalidate all artifacts dependent on changed source inputs."""
        s.requirements_assessment = None
        s.plan = None
        s.results = []
        s.package = None

    def load_context(self, payload: str, *, actor: str, role: Literal['manufacturing', 'qa'], reason: str) -> Snapshot:
        """Record human-supplied input changes and invalidate dependent approvals."""
        return self.load_context_from(lambda: payload, actor=actor, role=role, reason=reason)

    def load_context_from(self, loader: Callable[[], str], *, actor: str, role: str, reason: str,
                          requirements_service: RequirementsService | None = None, attempts: int = 1) -> Snapshot:
        """Load source data inside the transaction so adapter errors are persisted."""
        if attempts not in (1, 2):
            raise WorkflowError('Attempts must be 1 or 2')
        service = requirements_service or OfflineRequirements()
        def apply(s: Snapshot) -> None:
            """Validate input authority and record explicit human resolution intent."""
            if role not in {'manufacturing', 'qa'} or not reason.strip():
                raise WorkflowError('Human role and resolution/change reason required')
            context = parse_contract(PlanningContext, loader())
            assessment = parse_contract(AgentResponse[RequirementsDraft], service.extract(context.model_dump_json()))
            expected = RequirementsDraft(sop=context.sop, recipe=context.recipe)
            validate_response(assessment, source_gaps(expected))
            if assessment.output != expected:
                raise WorkflowError('Requirements Agent changed authoritative counts, thresholds or source evidence')
            self._clear(s)
            s.requirements_assessment = assessment
            s.context = context
            s.state = 'validated'
            s.resolution_required = False
        for attempt in range(attempts):
            try:
                return self._action('load_context', actor, role,
                                    json.dumps({'reason': reason, 'attempt': attempt + 1}),
                                    apply, invalidate_on_error=True)
            except WorkflowError:
                if attempt + 1 == attempts:
                    raise
        raise AssertionError('Unreachable')

    @staticmethod
    def _validate_plan(s: Snapshot, plan: SamplingPlan) -> None:
        """Reject changed obligations, stale revisions, unknown windows and occupancy."""
        if s.context is None:
            raise WorkflowError('Validated context required')
        if plan.revision <= s.last_revision:
            raise WorkflowError('Plan revision must increase monotonically')
        validate_plan(s.context, plan)

    def propose(self, service: ProposalService, *, attempts: int = 2) -> Snapshot:
        """Call and validate a service with at most two visible attempts."""
        if attempts not in (1, 2):
            raise WorkflowError('Attempts must be 1 or 2')
        for attempt in range(attempts):
            def apply(s: Snapshot) -> None:
                """Accept a proposal only in a valid controller-owned state."""
                if s.state not in {'validated', 'plan_proposed', 'blocked'} or s.context is None:
                    raise WorkflowError('Load/revalidate context before proposing')
                if s.resolution_required:
                    raise WorkflowError('Human source update/resolution required before reproposing')
                plan = parse_contract(SamplingPlan, service.propose(s.context.model_dump_json()))
                self._validate_plan(s, plan)
                s.plan = plan
                s.last_revision = plan.revision
                s.results = []
                s.package = None
                s.state = 'blocked' if plan.conflicts or any(isinstance(r.count, Unknown) or isinstance(r.threshold, Unknown) for r in plan.requirements) else 'plan_proposed'
                s.resolution_required = s.state == 'blocked'
            try:
                return self._action('propose', 'controller', 'controller', f'attempt {attempt+1}', apply)
            except WorkflowError:
                if attempt + 1 == attempts:
                    raise
        raise AssertionError('Unreachable')

    def decide_plan(self, payload: str) -> Snapshot:
        """Record exact-revision human acknowledgement; never waive conflicts."""
        def apply(s: Snapshot) -> None:
            """Check current proposal and reject repeated or stale decisions."""
            d = parse_contract(HumanPlanningDecision, payload)
            if s.plan is None or s.state not in {'plan_proposed', 'blocked'}:
                raise WorkflowError('No plan awaiting decision')
            if (d.plan_id, d.plan_revision) != (s.plan.plan_id, s.plan.revision) or d.decision_id in s.decision_ids:
                raise WorkflowError('Stale or repeated decision')
            if d.decision == 'allow':
                if s.state == 'blocked' or s.resolution_required:
                    raise WorkflowError('Allow cannot waive unmet obligations')
                s.state = 'plan_approved'
            else:
                s.state = 'blocked'
                s.resolution_required = True
            s.decision_ids.append(d.decision_id)
        actor, role = self._identity(payload)
        return self._action('decide_plan', actor, role, payload, apply)

    @staticmethod
    def _identity(payload: str) -> tuple[str, str]:
        """Extract display attribution without trusting it as authorization."""
        try:
            value = json.loads(payload)
            return str(value.get('actor_id', 'unknown')), str(value.get('role', 'unknown'))
        except (ValueError, AttributeError):
            return 'unknown', 'unknown'

    def collect(self, *, actor: str, role: str) -> Snapshot:
        """Record explicitly simulated collection, separately from approval."""
        def apply(s: Snapshot) -> None:
            """Require operational role and approval before simulated collection."""
            if role != 'manufacturing' or s.state != 'plan_approved':
                raise WorkflowError('Manufacturing and approved plan required')
            s.state = 'collection_simulated'
        return self._action('collect_simulated', actor, role, 'Synthetic collection only', apply)

    def receive_results(self, payload: str) -> Snapshot:
        """Record validated LIMS ingestion without automatically passing results."""
        def apply(s: Snapshot) -> None:
            """Gate ingestion on simulated collection and preserve all anomalies."""
            if s.state not in {'collection_simulated', 'results_received', 'review_ready', 'qa_approved', 'qa_rejected'}:
                raise WorkflowError('Simulated collection required before results')
            data = parse_contract(ResultBatch, payload)
            if len({r.result_id for r in data.results}) != len(data.results):
                raise WorkflowError('Duplicate result record IDs')
            s.results = data.results
            s.package = None
            s.state = 'results_received'
        return self._action('receive_results', 'controller', 'controller', payload, apply)

    def prepare_review(self, service: ReviewService) -> Snapshot:
        """Accept a review proposal only for the exact plan and result snapshot."""
        def apply(s: Snapshot) -> None:
            """Reject fabricated evidence or stale package revisions."""
            if s.state != 'results_received' or s.plan is None:
                raise WorkflowError('Results and plan required')
            package = parse_contract(QAReviewPackage, service.review(s.plan.model_dump_json(), ResultBatch(results=s.results).model_dump_json()))
            if package.plan != s.plan or package.results != s.results or package.revision <= s.last_package_revision:
                raise WorkflowError('Review package changed evidence or reused a revision')
            evaluation = evaluate_results(s.plan, s.results)
            if (sorted(package.findings, key=lambda f: f.finding_id) != evaluation.findings
                    or package.counts != evaluation.counts or package.sources != evaluation.sources):
                raise WorkflowError('Review service changed deterministic findings, counts or source evidence')
            s.package = package
            s.last_package_revision = package.revision
            s.state = 'review_ready'
        return self._action('prepare_review', 'controller', 'controller', 'single bounded service attempt', apply)

    def decide_qa(self, payload: str) -> Snapshot:
        """Require QA attribution and current complete evidence before final approval."""
        def apply(s: Snapshot) -> None:
            """Independently gate approval even if a review agent omitted findings."""
            d = parse_contract(HumanQADecision, payload)
            p = s.package
            if s.state != 'review_ready' or p is None:
                raise WorkflowError('No current package awaiting QA')
            if (d.package_id, d.package_revision, d.plan_id, d.plan_revision) != (p.package_id, p.revision, p.plan.plan_id, p.plan.revision) or d.decision_id in s.decision_ids:
                raise WorkflowError('Stale or repeated QA decision')
            if d.decision == 'approve':
                self._complete(s)
                s.state = 'qa_approved'
            elif d.decision == 'reject':
                s.state = 'qa_rejected'
            else:
                s.state = 'blocked'
                s.resolution_required = True
            s.decision_ids.append(d.decision_id)
        actor, role = self._identity(payload)
        return self._action('decide_qa', actor, role, payload, apply)

    @staticmethod
    def _complete(s: Snapshot) -> None:
        """Conservatively reject incomplete, mismatched or failing evidence."""
        assert s.plan is not None and s.package is not None
        if s.plan.conflicts or s.package.findings or len(s.results) != len(s.plan.samples):
            raise WorkflowError('Unresolved findings, conflicts, or missing results')
        by_sample = {r.sample_id: r for r in s.results}
        if len(by_sample) != len(s.results):
            raise WorkflowError('Duplicate sample results')
        requirements = {r.requirement_id: r for r in s.plan.requirements}
        comparisons = {'lt': lambda a,b: a < b, 'le': lambda a,b: a <= b, 'gt': lambda a,b: a > b, 'ge': lambda a,b: a >= b, 'eq': lambda a,b: a == b}
        for sample in s.plan.samples:
            r = by_sample.get(sample.sample_id)
            threshold = requirements[sample.requirement_id].threshold
            if r is None or (r.plan_id, r.plan_revision, r.room_id, r.sample_type) != (s.plan.plan_id, s.plan.revision, sample.room_id, sample.sample_type):
                raise WorkflowError('Missing or mismatched result')
            if isinstance(threshold, Unknown) or isinstance(r.value, Unknown) or r.unit != threshold.unit:
                raise WorkflowError('Unknown threshold/value or incompatible unit')
            if not sample.window.start <= r.collected_at <= sample.window.end:
                raise WorkflowError('Collection timestamp outside planned window')
            if not comparisons[threshold.operator](r.value, threshold.value):
                raise WorkflowError('Out-of-limit result requires resolution')
