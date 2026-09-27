"""Planning acceptance tests and independent rejection of unsafe proposals."""
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from cleanroom_os.adapters import FileInputAdapter, load_context
from cleanroom_os.contracts import ContractValidationError, PlanningContext, SamplingPlan
from cleanroom_os.controller import Controller, WorkflowError
from cleanroom_os.planning import DeterministicPlanner, generate_plan, validate_plan

ROOT = Path(__file__).resolve().parents[1] / 'fixtures' / 'mock-facility'


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.adapter = FileInputAdapter(ROOT)
        self.context = load_context(self.adapter)

    def context_with(self, edit, resolved=False):
        data = load_context(self.adapter, resolved=resolved).model_dump(mode='json')
        edit(data)
        return PlanningContext.model_validate(data)

    def reject(self, edit, code=None):
        data = generate_plan(self.context).model_dump(mode='json')
        edit(data)
        with self.assertRaises((ContractValidationError, ValueError)) as caught:
            validate_plan(self.context, SamplingPlan.model_validate(data))
        if code:
            self.assertIn(code, [i.code for i in caught.exception.issues])

    def test_baseline_and_stable_ids(self):
        plan = generate_plan(self.context)
        self.assertEqual(plan, generate_plan(self.context))
        self.assertEqual(plan.requirements, self.context.recipe.requirements)
        self.assertEqual([s.room_id for s in plan.samples], ['A', 'A', 'C', 'C'])
        self.assertEqual([c.room_id for c in plan.conflicts], ['B'])
        self.assertIn('unavailable_room', plan.conflicts[0].reason)
        self.assertEqual(plan.requirements[1].count, 2)
        for sample in plan.samples:
            w = sample.window
            if sample.room_id == 'A':
                self.assertEqual(w.start.hour, 9)
                self.assertLessEqual(w.end.minute, 30)
            else:
                self.assertGreater(w.start.hour * 60 + w.start.minute, 600)
            self.assertTrue(sample.sequencing_reason)
            for source in sample.sources:
                self.assertIsNotNone(self.adapter.resolve_source(source))
        updated = generate_plan(load_context(self.adapter, resolved=True), revision=2)
        self.assertEqual(updated.conflicts, [])
        self.assertTrue(set(s.sample_id for s in plan.samples) <= set(s.sample_id for s in updated.samples))
        self.assertEqual(updated.requirements, plan.requirements)

    def test_unknown_execution_and_availability_are_conflicts(self):
        contexts = [self.context_with(lambda d: d.pop('execution')),
                    self.context_with(lambda d: d['execution'].update(sample_minutes={
                        'status': 'unknown', 'reason': 'Not supplied', 'sources': d['execution']['sources']}))]
        for context in contexts:
            plan = generate_plan(context)
            self.assertEqual(plan.samples, [])
            self.assertEqual({c.requirement_id for c in plan.conflicts}, {'REQ-A', 'REQ-B', 'REQ-C'})
        missing = self.context_with(lambda d: d['schedule']['availability'].pop(2))
        plan = generate_plan(missing)
        self.assertTrue(any('missing_availability' in c.reason and c.room_id == 'C' for c in plan.conflicts))
        self.assertFalse(any(s.room_id == 'C' for s in plan.samples))

    def test_insufficient_window_and_resource_limits(self):
        context = self.context_with(lambda d: d['execution'].update(sample_minutes=20))
        plan = generate_plan(context)
        self.assertTrue(any(c.room_id == 'A' and 'insufficient_window' in c.reason for c in plan.conflicts))
        self.assertEqual(plan.requirements[0].count, 2)
        context = self.context_with(lambda d: d['execution']['technician_availability'].update(end='2026-09-27T09:30:00-04:00'))
        plan = generate_plan(context)
        self.assertFalse(any(s.room_id == 'C' for s in plan.samples))

    def test_occupancy_is_subtracted_including_setup(self):
        def edit(d):
            d['schedule']['occupancy'][2]['window']['end'] = '2026-09-27T10:20:00-04:00'
        context = self.context_with(edit)
        plan = generate_plan(context)
        first = next(s for s in plan.samples if s.room_id == 'C')
        self.assertEqual(first.window.start.minute, 25)
        validate_plan(context, plan)

    def test_reject_overlap_access_setup_duration_and_evidence(self):
        self.reject(lambda d: d['samples'][1].update(window=d['samples'][0]['window']), 'resource_overlap')
        self.reject(lambda d: d['samples'][0]['window'].update(
            start='2026-09-27T09:00:00-04:00', end='2026-09-27T09:05:00-04:00'), 'inaccessible_visit')
        self.reject(lambda d: d['samples'][0]['window'].update(end='2026-09-27T09:09:00-04:00'), 'duration_mismatch')
        self.reject(lambda d: d['samples'][2]['window'].update(
            start='2026-09-27T09:55:00-04:00', end='2026-09-27T10:00:00-04:00'), 'occupied_visit')
        self.reject(lambda d: d['samples'][0]['sources'].pop(-2), 'missing_execution_evidence')

    def test_reject_missing_and_altered_obligations(self):
        self.reject(lambda d: d['samples'].pop(0))
        self.reject(lambda d: d['requirements'][0]['threshold'].update(value=100), 'changed_obligations')
        self.reject(lambda d: d['requirements'][0].update(count=1))
        self.reject(lambda d: d['samples'][0].update(sample_type='surface'))
        self.reject(lambda d: (d['requirements'].pop(1), d['conflicts'].clear()), 'changed_obligations')

    def test_validator_accepts_different_safe_producer_order(self):
        context = load_context(self.adapter, resolved=True)
        plan = generate_plan(context)
        # A future producer may choose a later safe C slot; no canonical-plan comparison.
        for sample in plan.samples:
            if sample.room_id == 'C':
                sample.window = sample.window.model_copy(update={
                    'start': sample.window.start + timedelta(minutes=20),
                    'end': sample.window.end + timedelta(minutes=20)})
        validate_plan(context, plan)

    def test_partial_human_update_new_revision_retains_other_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = Controller(Path(tmp) / 'run.sqlite')
            def load(context):
                controller.load_context(context.model_dump_json(), actor='MFG-1', role='manufacturing', reason='Human source update')
            # Both B and C blocked; only B receives a valid replacement window.
            initial = self.context_with(lambda d: d['schedule']['availability'][2].update(windows=[]))
            load(initial)
            first = controller.propose(DeterministicPlanner(1)).plan
            self.assertEqual({c.room_id for c in first.conflicts}, {'B', 'C'})
            replacement = self.context_with(lambda d: d['schedule']['availability'][2].update(windows=[]), resolved=True)
            load(replacement)
            second = controller.propose(DeterministicPlanner(2)).plan
            self.assertEqual({c.room_id for c in second.conflicts}, {'C'})
            self.assertEqual(second.requirements, first.requirements)
            self.assertEqual(second.revision, first.revision + 1)
            self.assertEqual(controller.snapshot().state, 'blocked')
            self.assertGreater(len(controller.events()), 3)
            with self.assertRaises(WorkflowError):
                controller.propose(DeterministicPlanner(3))

    def test_travel_from_starting_room_is_enforced(self):
        context = self.context_with(lambda d: d['execution'].update(starting_room_id='B'))
        plan = generate_plan(context)
        self.assertEqual(plan.samples[0].window.start.minute, 10)
        forged = generate_plan(self.context)
        with self.assertRaises(ContractValidationError) as caught:
            validate_plan(context, forged)
        self.assertIn('resource_overlap', [i.code for i in caught.exception.issues])

    def test_conflicting_authority_is_structured_input_failure(self):
        import json
        from cleanroom_os.contracts import parse_contract
        data = self.context.model_dump(mode='json')
        data['recipe']['requirements'][0]['count'] = 3
        with self.assertRaises(ContractValidationError) as caught:
            parse_contract(PlanningContext, json.dumps(data))
        self.assertIn('Conflicting authoritative requirements', caught.exception.issues[0].message)


if __name__ == '__main__':
    unittest.main()
