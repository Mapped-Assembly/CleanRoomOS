"""Deterministic LIMS matching and threshold evidence, with no approval authority."""
from collections import Counter, defaultdict
from hashlib import sha256
import json
import operator

from cleanroom_os.contracts import (
    EvaluationCounts, LIMSResult, QAFinding, QAReviewPackage, ResultBatch,
    ResultEvaluation, SamplingPlan, SourceReference, Unknown, parse_contract,
)

COMPARISONS = {'lt': operator.lt, 'le': operator.le, 'gt': operator.gt,
               'ge': operator.ge, 'eq': operator.eq}


def _sources(*groups: list[SourceReference]) -> list[SourceReference]:
    """Deduplicate references without losing source versions or locators."""
    refs = {(s.kind, s.source_id, s.version, s.locator): s for group in groups for s in group}
    return [refs[key] for key in sorted(refs)]


def evaluate_results(plan: SamplingPlan, results: list[LIMSResult]) -> ResultEvaluation:
    """Match exactly; never reassign stale records or infer thresholds/conversions.

    Callers own approval state. The controller supplies its approved/collected plan;
    direct calls can also describe blocked proposals without authorizing collection.
    Each import is a complete snapshot, not an accumulating stream.
    """
    plan = parse_contract(SamplingPlan, plan.model_dump_json())
    batch = parse_contract(ResultBatch, ResultBatch(results=results).model_dump_json())
    samples = {s.sample_id: s for s in plan.samples}
    requirements = {r.requirement_id: r for r in plan.requirements}
    by_sample = defaultdict(list)
    for result in batch.results:
        by_sample[result.sample_id].append(result)
    findings = []

    def finding(kind, code, description, *, sample_id=None, requirement_id=None,
                records=(), evidence=()):
        """Stable identities change with the finding's actual evidence, not evaluation time."""
        data = dict(kind=kind, code=code, description=description,
                    sample_id=sample_id, requirement_id=requirement_id,
                    result_ids=sorted(r.result_id for r in records),
                    sources=_sources(list(evidence), *(r.sources for r in records)))
        encoded = json.dumps({**data, 'sources': [s.model_dump() for s in data['sources']]},
                             sort_keys=True, separators=(',', ':'))
        identity = f'{plan.plan_id}\0{plan.revision}\0{encoded}'
        findings.append(QAFinding(finding_id='FINDING-' + sha256(identity.encode()).hexdigest()[:32], **data))

    planned_counts = Counter(s.requirement_id for s in plan.samples)
    blocked = unknown_counts = 0
    for requirement in plan.requirements:
        rid = requirement.requirement_id
        evidence = _sources(requirement.sources, requirement.threshold.sources)
        conflicts = [c for c in plan.conflicts if c.requirement_id == rid]
        if isinstance(requirement.count, Unknown):
            unknown_counts += 1
            outstanding = None
            evidence = _sources(evidence, requirement.count.sources)
        else:
            outstanding = requirement.count - planned_counts[rid]
            blocked += outstanding
        if outstanding is None or outstanding > 0 or conflicts:
            count_text = 'Unknown required count' if outstanding is None else f'{outstanding} required samples not scheduled'
            reason = '; '.join(c.reason for c in conflicts)
            finding('blocked', 'uncollected_obligation', f'{count_text}; unresolved planning obligations. {reason}',
                    requirement_id=rid, evidence=_sources(evidence, *(c.sources for c in conflicts)))
        if isinstance(requirement.threshold, Unknown):
            finding('unknown', 'unknown_threshold', f'Authoritative threshold unknown: {requirement.threshold.reason}',
                    requirement_id=rid, evidence=evidence)

    matched_ids = set()
    matched_records = mismatched_records = unmatched_records = duplicate_records = 0
    for sid, records in sorted(by_sample.items()):
        sample = samples.get(sid)
        requirement = requirements[sample.requirement_id] if sample else None
        evidence = _sources(sample.sources, requirement.sources, requirement.threshold.sources) if sample else []
        rid = requirement.requirement_id if requirement else None
        if len(records) > 1:
            duplicate_records += len(records) - 1
            finding('duplicate', 'multiple_results', 'Multiple distinct LIMS records reference this sample; all are retained for review.',
                    sample_id=sid, requirement_id=rid, records=records, evidence=evidence)
        for result in sorted(records, key=lambda r: r.result_id):
            if sample is None:
                unmatched_records += 1
                finding('unmatched', 'unknown_sample_id', 'LIMS sample ID is absent from the exact supplied plan; record was not reassigned.',
                        sample_id=sid, records=[result])
                if (result.plan_id, result.plan_revision) != (plan.plan_id, plan.revision):
                    finding('mismatch', 'plan_identity_mismatch',
                            f'Received plan {result.plan_id} revision {result.plan_revision}; '
                            f'expected {plan.plan_id} revision {plan.revision}.',
                            sample_id=sid, records=[result],
                            evidence=_sources(*(r.sources for r in plan.requirements)))
                continue
            differences = []
            for field, expected in [('plan_id', plan.plan_id), ('plan_revision', plan.revision),
                                    ('room_id', sample.room_id), ('sample_type', sample.sample_type)]:
                actual = getattr(result, field)
                if actual != expected:
                    differences.append(f'{field}: received {actual!r}, expected {expected!r}')
            if not sample.window.start <= result.collected_at <= sample.window.end:
                differences.append(f'collected_at {result.collected_at.isoformat()} outside planned collection window '
                                   f'{sample.window.start.isoformat()} to {sample.window.end.isoformat()}')
            if differences:
                mismatched_records += 1
                finding('mismatch', 'collection_context_mismatch', '; '.join(differences),
                        sample_id=sid, requirement_id=rid, records=[result], evidence=evidence)
                continue  # Never apply this plan's threshold to a different collection context.
            matched_records += 1
            matched_ids.add(sid)
            threshold = requirement.threshold
            if isinstance(result.value, Unknown):
                finding('unknown', 'unknown_measurement', f'Measurement unknown: {result.value.reason}',
                        sample_id=sid, requirement_id=rid, records=[result],
                        evidence=_sources(evidence, result.value.sources))
            if isinstance(threshold, Unknown):
                continue  # Already reported at obligation level, even when no results arrived.
            if result.unit != threshold.unit:
                finding('mismatch', 'incompatible_unit',
                        f'Received {result.unit}; authoritative threshold uses {threshold.unit}. No conversion assumed.',
                        sample_id=sid, requirement_id=rid, records=[result], evidence=evidence)
            elif not isinstance(result.value, Unknown) and not COMPARISONS[threshold.operator](result.value, threshold.value):
                finding('out_of_limit', 'threshold_violation',
                        f'Received {result.value} {result.unit}; required {threshold.operator} {threshold.value} {threshold.unit}.',
                        sample_id=sid, requirement_id=rid, records=[result], evidence=evidence)

    missing = mismatched_samples = 0
    for sample in plan.samples:
        if sample.sample_id not in by_sample:
            missing += 1
            requirement = requirements[sample.requirement_id]
            finding('missing', 'expected_result_missing', 'Scheduled sample has no received LIMS record.',
                    sample_id=sample.sample_id, requirement_id=sample.requirement_id,
                    evidence=_sources(sample.sources, requirement.sources, requirement.threshold.sources))
        elif sample.sample_id not in matched_ids:
            mismatched_samples += 1

    counts = EvaluationCounts(
        known_required=sum(r.count for r in plan.requirements if not isinstance(r.count, Unknown)),
        expected=len(plan.samples), received=len(batch.results), matched=len(matched_ids),
        missing=missing, mismatched_samples=mismatched_samples, blocked=blocked,
        unknown_count_requirements=unknown_counts, matched_records=matched_records,
        mismatched_records=mismatched_records, unmatched_records=unmatched_records,
        duplicate_records=duplicate_records)
    sources = _sources(*(r.sources for r in plan.requirements),
                       *(r.threshold.sources for r in plan.requirements),
                       *(s.sources for s in plan.samples), *(r.sources for r in batch.results),
                       *(f.sources for f in findings))
    return ResultEvaluation(plan_id=plan.plan_id, plan_revision=plan.revision, counts=counts,
                            findings=sorted(findings, key=lambda f: f.finding_id), sources=sources)


class DeterministicReviewer:
    """Produce a review package with exact input evidence and deterministic findings."""

    def __init__(self, revision: int, package_id: str = 'QA-PACKAGE') -> None:
        self.revision = revision
        self.package_id = package_id

    def review(self, plan_json: str, results_json: str) -> str:
        plan = parse_contract(SamplingPlan, plan_json)
        results = parse_contract(ResultBatch, results_json)
        evaluation = evaluate_results(plan, results.results)
        return QAReviewPackage(package_id=self.package_id, revision=self.revision, plan=plan,
                               results=results.results, findings=evaluation.findings,
                               counts=evaluation.counts, sources=evaluation.sources).model_dump_json()
