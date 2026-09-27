"""Exact-revision result matching, reconciliation and evidence acceptance tests."""
import json
import unittest
from datetime import timedelta
from pathlib import Path

from cleanroom_os.adapters import FileInputAdapter, Fixture, load_context
from cleanroom_os.contracts import (
    ContractValidationError, EvaluationCounts, LIMSResult, QAReviewPackage,
    ResultBatch, SamplingPlan, Unknown, parse_contract,
)
from cleanroom_os.evaluation import DeterministicReviewer, evaluate_results
from cleanroom_os.planning import generate_plan

ROOT = Path(__file__).resolve().parents[1] / 'fixtures' / 'mock-facility'


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.adapter = FileInputAdapter(ROOT)
        self.plan = parse_contract(Fixture[SamplingPlan], (ROOT / 'plan-resolved.json').read_text()).data
        self.normal = self.adapter.load_lims(anomalies=False)

    def test_normal_exact_matches_have_no_findings_or_decision(self):
        evaluation = evaluate_results(self.plan, self.normal)
        self.assertEqual(evaluation.findings, [])
        self.assertEqual(evaluation.counts.model_dump(), dict(
            known_required=6, expected=6, received=6, matched=6, missing=0,
            mismatched_samples=0, blocked=0, unknown_count_requirements=0,
            matched_records=6, mismatched_records=0, unmatched_records=0, duplicate_records=0))
        self.assertEqual((evaluation.plan_id, evaluation.plan_revision), (self.plan.plan_id, 2))
        self.assertNotIn('decision', evaluation.model_dump())
        self.assertNotIn('approved', evaluation.model_dump())

    def test_every_fixture_anomaly_and_counts_match_oracle(self):
        results = self.adapter.load_lims()
        before = [r.model_dump_json() for r in results]
        evaluation = evaluate_results(self.plan, results)
        oracle = json.loads((ROOT / 'expected-findings.json').read_text())['data']
        for kind in ['missing', 'unmatched', 'duplicate', 'mismatch', 'out_of_limit']:
            self.assertEqual(sorted(f.sample_id for f in evaluation.findings if f.kind == kind), oracle[kind])
        self.assertEqual(len(evaluation.findings), 5)
        self.assertEqual([r.model_dump_json() for r in results], before)
        self.assertEqual(evaluation.counts.model_dump(), dict(
            known_required=6, expected=6, received=7, matched=4, missing=1,
            mismatched_samples=1, blocked=0, unknown_count_requirements=0,
            matched_records=5, mismatched_records=1, unmatched_records=1, duplicate_records=1))

    def test_all_findings_have_resolvable_source_evidence(self):
        results = self.adapter.load_lims()
        evaluation = evaluate_results(self.plan, results)
        by_id = {r.result_id: r for r in results}
        for finding in evaluation.findings:
            for source in finding.sources:
                self.assertIsNotNone(self.adapter.resolve_source(source))
            for rid in finding.result_ids:
                for source in by_id[rid].sources:
                    self.assertIn(source, finding.sources)
            if finding.requirement_id:
                requirement = next(r for r in self.plan.requirements if r.requirement_id == finding.requirement_id)
                for source in [*requirement.sources, *requirement.threshold.sources]:
                    self.assertIn(source, finding.sources)
        out = next(f for f in evaluation.findings if f.kind == 'out_of_limit')
        self.assertEqual(out.requirement_id, 'REQ-C')
        self.assertEqual(out.result_ids, ['RES-3'])

    def test_extra_and_duplicate_records_never_mask_missing(self):
        data = [r.model_dump(mode='json') for r in self.normal[:-1]]
        data.append({**data[0], 'result_id': 'EXTRA', 'sample_id': 'NOT-PLANNED'})
        data.append({**data[0], 'result_id': 'DUPLICATE'})
        evaluation = evaluate_results(self.plan, [LIMSResult.model_validate(r) for r in data])
        self.assertEqual(evaluation.counts.received, 7)
        self.assertEqual(evaluation.counts.missing, 1)
        self.assertEqual(evaluation.counts.matched, 5)
        self.assertEqual(evaluation.counts.duplicate_records, 1)
        self.assertEqual(evaluation.counts.unmatched_records, 1)

    def test_exact_context_fields_and_time_are_required(self):
        variants = [dict(plan_revision=1), dict(plan_id='OTHER-PLAN'), dict(room_id='C'),
                    dict(sample_type='surface'),
                    dict(collected_at=self.plan.samples[0].window.start - timedelta(seconds=1)),
                    dict(collected_at=self.plan.samples[0].window.end + timedelta(seconds=1))]
        for changes in variants:
            with self.subTest(changes=changes):
                result = LIMSResult.model_validate(self.normal[0].model_dump() | changes)
                evaluation = evaluate_results(self.plan, [result, *self.normal[1:]])
                self.assertEqual(evaluation.counts.matched, 5)
                self.assertEqual(evaluation.counts.mismatched_samples, 1)
                self.assertEqual(evaluation.counts.missing, 0)
                self.assertEqual([f.code for f in evaluation.findings], ['collection_context_mismatch'])
                # Matching never silently rewrites the result revision or identity.
                for key, value in changes.items():
                    self.assertEqual(getattr(result, key), value)

    def test_stale_and_current_records_for_same_sample_remain_visible(self):
        stale = LIMSResult.model_validate(self.normal[0].model_dump() | dict(result_id='OLD', plan_revision=1))
        evaluation = evaluate_results(self.plan, [*self.normal, stale])
        self.assertEqual(evaluation.counts.matched, 6)
        self.assertEqual(evaluation.counts.matched_records, 6)
        self.assertEqual(evaluation.counts.mismatched_records, 1)
        self.assertEqual({f.kind for f in evaluation.findings}, {'duplicate', 'mismatch'})

    def test_unplanned_stale_record_reports_both_identity_failures(self):
        result = LIMSResult.model_validate(self.normal[0].model_dump() | dict(sample_id='OLD-SAMPLE', plan_revision=1))
        evaluation = evaluate_results(self.plan, [result])
        self.assertTrue({'unknown_sample_id', 'plan_identity_mismatch'} <= {f.code for f in evaluation.findings})
        self.assertEqual(evaluation.counts.unmatched_records, 1)
        self.assertEqual(evaluation.counts.mismatched_records, 0)  # Record partitions do not double-count findings.
        self.assertEqual(evaluation.counts.missing, 6)

    def test_incompatible_units_are_findings_without_conversion(self):
        result = LIMSResult.model_validate(self.normal[0].model_dump() | dict(unit='CFU', value=0))
        evaluation = evaluate_results(self.plan, [result, *self.normal[1:]])
        self.assertEqual([f.code for f in evaluation.findings], ['incompatible_unit'])
        self.assertEqual(evaluation.counts.matched, 6)  # Identity match is not a measurement pass.

    def test_unknown_value_and_threshold_are_findings(self):
        result = LIMSResult.model_validate(self.normal[0].model_dump() | dict(value={
            'status': 'unknown', 'reason': 'Lab result pending', 'sources': [s.model_dump() for s in self.normal[0].sources]}))
        evaluation = evaluate_results(self.plan, [result, *self.normal[1:]])
        self.assertEqual([f.code for f in evaluation.findings], ['unknown_measurement'])
        plan = self.plan.model_copy(deep=True)
        plan.requirements[0].threshold = Unknown(status='unknown', reason='Limit not supplied',
                                                  sources=plan.requirements[0].sources)
        evaluation = evaluate_results(plan, self.normal)
        self.assertEqual([f.code for f in evaluation.findings], ['unknown_threshold'])
        self.assertEqual(evaluation.findings[0].requirement_id, 'REQ-A')

    def test_all_authoritative_comparators_and_boundaries(self):
        for comparison, value, passes in [('lt', 10, False), ('lt', 9, True), ('le', 10, True),
                                          ('gt', 10, False), ('gt', 11, True), ('ge', 10, True),
                                          ('eq', 10, True), ('eq', 9, False)]:
            with self.subTest(comparison=comparison, value=value):
                plan = self.plan.model_copy(deep=True)
                plan.requirements[0].threshold.operator = comparison
                records = [LIMSResult.model_validate(r.model_dump() | {'value': value}) for r in self.normal[:2]] + self.normal[2:]
                evaluation = evaluate_results(plan, records)
                self.assertEqual(len([f for f in evaluation.findings if f.kind == 'out_of_limit']), 0 if passes else 2)

    def test_failing_duplicate_measurement_is_not_discarded(self):
        high = LIMSResult.model_validate(self.normal[0].model_dump() | dict(result_id='DUP-HIGH', value=999))
        evaluation = evaluate_results(self.plan, [*self.normal, high])
        self.assertEqual({f.kind for f in evaluation.findings}, {'duplicate', 'out_of_limit'})

    def test_blocked_unknown_and_missing_counts_are_distinct(self):
        plan = generate_plan(load_context(self.adapter))
        evaluation = evaluate_results(plan, [])
        self.assertEqual((evaluation.counts.known_required, evaluation.counts.expected,
                          evaluation.counts.blocked, evaluation.counts.missing), (6, 4, 2, 4))
        self.assertEqual([f.requirement_id for f in evaluation.findings if f.kind == 'blocked'], ['REQ-B'])
        self.assertFalse(any(f.kind == 'missing' and f.requirement_id == 'REQ-B' for f in evaluation.findings))
        plan.requirements[1].count = Unknown(status='unknown', reason='Count not supplied', sources=plan.requirements[1].sources)
        evaluation = evaluate_results(plan, [])
        self.assertEqual(evaluation.counts.unknown_count_requirements, 1)
        self.assertEqual(evaluation.counts.known_required, 4)
        self.assertEqual(evaluation.counts.blocked, 0)  # Unknown is not invented as a numeric count.

    def test_partial_requirement_preserves_unscheduled_obligations(self):
        data = self.plan.model_dump(mode='json')
        data['samples'] = data['samples'][:-1]
        data['conflicts'] = [dict(conflict_id='B-PARTIAL', requirement_id='REQ-B', room_id='B',
                                  reason='One sample could not be scheduled', sources=data['requirements'][1]['sources'])]
        evaluation = evaluate_results(SamplingPlan.model_validate(data), self.normal[:-1])
        self.assertEqual((evaluation.counts.expected, evaluation.counts.matched, evaluation.counts.blocked), (5, 5, 1))
        self.assertEqual({f.kind for f in evaluation.findings}, {'blocked'})

    def test_repeated_or_reordered_evaluation_has_stable_findings(self):
        results = self.adapter.load_lims()
        first = evaluate_results(self.plan, results)
        self.assertEqual(first, evaluate_results(self.plan, results))
        self.assertEqual(first, evaluate_results(self.plan, list(reversed(results))))
        self.assertEqual(len(first.findings), len({f.finding_id for f in first.findings}))

    def test_ambiguous_record_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_results(self.plan, [self.normal[0], self.normal[0]])
        with self.assertRaises(ContractValidationError):
            parse_contract(ResultBatch, json.dumps({'results': [self.normal[0].model_dump(mode='json')] * 2}))

    def test_counts_reject_nonreconciling_values(self):
        data = evaluate_results(self.plan, self.normal).counts.model_dump()
        for field in ['known_required', 'expected', 'received']:
            with self.subTest(field=field), self.assertRaises(ValueError):
                EvaluationCounts.model_validate(data | {field: data[field] + 1})

    def test_reviewer_preserves_plan_results_and_source_versions(self):
        results = self.adapter.load_lims()
        payload = DeterministicReviewer(7).review(self.plan.model_dump_json(), ResultBatch(results=results).model_dump_json())
        package = parse_contract(QAReviewPackage, payload)
        self.assertEqual(package.plan, self.plan)
        self.assertEqual(package.results, results)
        self.assertEqual(package.revision, 7)
        self.assertEqual(package.findings, evaluate_results(self.plan, results).findings)
        for result in results:
            for ref in result.sources:
                self.assertIn(ref, package.sources)


if __name__ == '__main__':
    unittest.main()
