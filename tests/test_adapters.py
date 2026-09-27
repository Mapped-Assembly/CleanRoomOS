"""Verify complete local scenarios, actual evidence, and intentional anomalies."""
import json
import tempfile
import unittest
from collections import Counter
from datetime import timedelta
from pathlib import Path

from cleanroom_os.adapters import FileInputAdapter, Fixture, load_context
from cleanroom_os.contracts import ContractValidationError, SamplingPlan, SourceReference, parse_contract

ROOT = Path(__file__).resolve().parents[1] / 'fixtures' / 'mock-facility'


class AdapterTests(unittest.TestCase):
    """Exercise mock input semantics independently of the future planner."""

    def setUp(self) -> None:
        """Load the explicit fixture directory without credentials."""
        self.adapter = FileInputAdapter(ROOT)

    def plan(self, resolved: bool) -> SamplingPlan:
        """Read a hand-authored expected-plan oracle, never generate a plan."""
        name = 'plan-resolved.json' if resolved else 'plan-blocked.json'
        return parse_contract(Fixture[SamplingPlan], (ROOT / name).read_text()).data

    def test_blocked_and_resolved_inputs(self) -> None:
        """The baseline holds B; only the explicit variant opens availability."""
        baseline = load_context(self.adapter)
        resolved = load_context(self.adapter, resolved=True)
        self.assertEqual(baseline.recipe, resolved.recipe)
        self.assertEqual(baseline.sop, resolved.sop)
        self.assertEqual(len(baseline.rooms), 3)
        self.assertEqual(baseline.schedule.availability[1].windows, [])
        b_occupancy = baseline.schedule.occupancy[1].window
        self.assertEqual(b_occupancy, baseline.schedule.shift)
        self.assertEqual(resolved.schedule.availability[1].windows[0].start.hour, 11)
        update = self.adapter.load_human_update()
        self.assertEqual(update.previous_schedule_version, baseline.schedule.version)
        self.assertEqual(update.replacement_schedule_version, resolved.schedule.version)
        blocked = self.plan(False)
        self.assertEqual([c.room_id for c in blocked.conflicts], ['B'])
        self.assertEqual(next(r.count for r in blocked.requirements if r.room_id == 'B'), 2)
        self.assertEqual([s.room_id for s in blocked.samples], ['A', 'A', 'C', 'C'])
        self.assertEqual(self.plan(True).conflicts, [])

    def test_expected_plans_fit_known_constraints(self) -> None:
        """Oracles respect access, occupancy, sample/setup/travel duration and shift."""
        execution = self.adapter.load_facility().execution
        for resolved in (False, True):
            context = load_context(self.adapter, resolved=resolved)
            plan = self.plan(resolved)
            self.assertEqual(plan.requirements, context.recipe.requirements)
            previous = None
            for sample in plan.samples:
                w = sample.window
                self.assertEqual(w.end-w.start, timedelta(minutes=execution.sample_minutes))
                available = next(a.windows for a in context.schedule.availability if a.room_id == sample.room_id)
                self.assertTrue(any(a.start <= w.start < w.end <= a.end for a in available))
                self.assertTrue(context.schedule.shift.start <= w.start < w.end <= context.schedule.shift.end)
                for occupied in context.schedule.occupancy:
                    if occupied.room_id == sample.room_id:
                        self.assertFalse(w.start < occupied.window.end and occupied.window.start < w.end)
                if previous is None or previous.room_id != sample.room_id:
                    setup_start = w.start-timedelta(minutes=execution.setup_minutes_per_room)
                    self.assertTrue(any(a.start <= setup_start for a in available))
                    if previous:
                        self.assertGreaterEqual(setup_start-previous.window.end, timedelta(minutes=execution.travel_minutes_between_rooms))
                else:
                    self.assertGreaterEqual(w.start, previous.window.end)
                previous = sample

    def test_lims_variants_and_expected_anomalies(self) -> None:
        """Fixture anomalies are real discrepancies, not merely descriptive labels."""
        plan = self.plan(True)
        normal = self.adapter.load_lims(anomalies=False)
        self.assertEqual(Counter(r.sample_id for r in normal), Counter(s.sample_id for s in plan.samples))
        for r in normal:
            self.assertEqual((r.plan_id, r.plan_revision), (plan.plan_id, plan.revision))
        samples = {s.sample_id: s for s in plan.samples}
        anomalous = self.adapter.load_lims()
        expected = json.loads((ROOT/'expected-findings.json').read_text())['data']
        counts = Counter(r.sample_id for r in anomalous)
        self.assertEqual(sorted(set(samples)-set(counts)), expected['missing'])
        self.assertEqual(sorted(k for k,v in counts.items() if v>1), expected['duplicate'])
        self.assertEqual(sorted(set(counts)-set(samples)), expected['unmatched'])
        self.assertEqual(sorted(r.sample_id for r in anomalous if r.sample_id in samples and r.room_id != samples[r.sample_id].room_id), expected['mismatch'])
        limits = {r.room_id:r.threshold for r in plan.requirements}
        self.assertEqual(sorted(r.sample_id for r in anomalous if r.value > limits[r.room_id].value), expected['out_of_limit'])
        self.assertEqual(len({r.result_id for r in anomalous}), len(anomalous))

    def test_all_evidence_resolves(self) -> None:
        """Every serialized source reference points to an actual fixture node."""
        def walk(value: object) -> None:
            """Traverse fixture JSON and resolve every embedded reference."""
            if isinstance(value, dict):
                if {'kind','source_id','version','locator'} <= value.keys():
                    evidence = self.adapter.resolve_source(SourceReference.model_validate(value))
                    self.assertIsNotNone(evidence)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        for path in ROOT.glob('*.json'):
            payload = json.loads(path.read_text())
            self.assertIs(payload['synthetic'], True)
            walk(payload)

    def test_invalid_or_missing_fixture_fails_visibly(self) -> None:
        """Never substitute defaults or retrieve live data after input failure."""
        with tempfile.TemporaryDirectory() as directory:
            adapter = FileInputAdapter(Path(directory))
            with self.assertRaises(FileNotFoundError):
                adapter.load_sop()
            (Path(directory)/'sop.json').write_text('{"synthetic":true,"data":{}}')
            with self.assertRaises(ContractValidationError):
                adapter.load_sop()
        ref = SourceReference(kind='sop',source_id='SYN-SAMPLING-001',version='missing',locator='/data')
        with self.assertRaises(ValueError):
            self.adapter.resolve_source(ref)


if __name__ == '__main__':
    unittest.main()
