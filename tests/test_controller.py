"""Verify enforced workflow gates, durability and bounded service failures."""
import json
import tempfile
import unittest
from pathlib import Path

from cleanroom_os.adapters import FileInputAdapter, load_context
from cleanroom_os.controller import Controller, ResultBatch, WorkflowError
from cleanroom_os.mock_services import FixturePlanner, FixtureReviewer
from cleanroom_os.evaluation import DeterministicReviewer

ROOT = Path(__file__).resolve().parents[1] / 'fixtures' / 'mock-facility'


class BrokenPlanner:
    """Track calls while returning malformed model output."""

    def __init__(self) -> None:
        """Start with no attempts."""
        self.calls = 0

    def propose(self, context_json: str) -> str:
        """Return prose instead of a structured plan."""
        self.calls += 1
        return 'Everything looks good; approved!'


class ControllerTests(unittest.TestCase):
    """Exercise actual SQLite transactions and workflow invariants."""

    def setUp(self) -> None:
        """Allocate an isolated durable store per scenario."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'run.sqlite'
        self.controller = Controller(self.path)
        self.adapter = FileInputAdapter(ROOT)
        self.planner = FixturePlanner(ROOT)

    def context(self, resolved: bool = True) -> None:
        """Record an explicit synthetic manufacturing input update."""
        self.controller.load_context(load_context(self.adapter, resolved=resolved).model_dump_json(),
                                     actor='MFG-1', role='manufacturing', reason='Explicit synthetic schedule selection')

    def decision(self, *, qa: bool = False, **changes: object) -> str:
        """Create revision-bound self-reported demo decision payloads."""
        snap = self.controller.snapshot()
        data = dict(decision_id='QA-DECISION' if qa else 'PLAN-DECISION', plan_id=snap.plan.plan_id,
                    plan_revision=snap.plan.revision, actor_id='QA-1' if qa else 'MFG-1', role='qa' if qa else 'manufacturing',
                    decision='approve' if qa else 'allow', rationale='Synthetic human review', decided_at='2026-09-27T17:00:00-04:00',
                    sources=[dict(kind='human',source_id='HUMAN-1',version='1',locator='demo-decision')])
        if qa:
            data.update(package_id=snap.package.package_id, package_revision=snap.package.revision)
        return json.dumps(data | changes)

    def ready(self, anomalies: bool = False) -> None:
        """Run the synthetic approved-plan path through review readiness."""
        self.context()
        self.controller.propose(self.planner)
        self.controller.decide_plan(self.decision())
        self.controller.collect(actor='TECH-1', role='manufacturing')
        self.controller.receive_results(ResultBatch(results=self.adapter.load_lims(anomalies=anomalies)).model_dump_json())
        self.controller.prepare_review(FixtureReviewer(1))

    def test_happy_path_and_restart(self) -> None:
        """Restart preserves evidence and final decisions without an LLM."""
        self.ready()
        self.assertEqual(self.controller.snapshot().state, 'review_ready')
        self.controller = Controller(self.path)
        self.assertEqual(self.controller.snapshot().state, 'review_ready')
        decision = self.decision(qa=True)
        self.controller.decide_qa(decision)
        reopened = Controller(self.path)
        self.assertEqual(reopened.snapshot().state, 'qa_approved')
        self.assertIn('QA-DECISION', reopened.snapshot().decision_ids)
        self.assertEqual(json.loads(reopened.events()[-1]['details']), json.loads(decision))
        with self.assertRaises(WorkflowError):
            reopened.decide_qa(decision)
        self.assertEqual(reopened.snapshot().state, 'qa_approved')

    def test_blocked_requires_human_resolution(self) -> None:
        """Allow and repeated agent calls cannot drop blocked obligations."""
        self.context(False)
        self.controller.propose(self.planner)
        self.assertEqual(self.controller.snapshot().state, 'blocked')
        self.assertEqual(self.controller.snapshot().plan.requirements[1].count, 2)
        with self.assertRaises(WorkflowError):
            self.controller.decide_plan(self.decision())
        with self.assertRaises(WorkflowError):
            self.controller.propose(self.planner)
        self.controller = Controller(self.path)
        self.assertEqual(self.controller.snapshot().state, 'blocked')
        self.context(True)
        self.controller.propose(self.planner)
        self.controller.decide_plan(self.decision())
        self.assertEqual(self.controller.snapshot().state, 'plan_approved')

    def test_input_adapter_failure_is_persisted(self) -> None:
        """Missing source files are visible in the durable error ledger."""
        def missing() -> str:
            """Simulate an input adapter failure."""
            raise FileNotFoundError('Missing SOP source')
        with self.assertRaises(WorkflowError):
            self.controller.load_context_from(missing, actor='MFG-1',role='manufacturing',reason='Load sources')
        self.assertIn('FileNotFoundError', self.controller.events()[-1]['details'])
        self.assertEqual(self.controller.snapshot().state, 'context_pending')

    def test_missing_context_and_invalid_transitions(self) -> None:
        """State cannot jump to collection, results or review without prerequisites."""
        for action in [lambda: self.controller.propose(self.planner),
                       lambda: self.controller.collect(actor='TECH-1',role='manufacturing'),
                       lambda: self.controller.receive_results('{"results":[]}'),
                       lambda: self.controller.prepare_review(FixtureReviewer(1))]:
            with self.assertRaises(WorkflowError):
                action()
            self.assertEqual(self.controller.snapshot().state, 'context_pending')
        self.assertTrue(all(e['status']=='error' for e in self.controller.events()))

    def test_revision_and_role_checks(self) -> None:
        """Stale or non-QA decisions remain visible and do not advance state."""
        self.ready()
        for changes in [dict(plan_revision=1), dict(package_revision=99), dict(role='manufacturing')]:
            with self.assertRaises(WorkflowError):
                self.controller.decide_qa(self.decision(qa=True, **changes))
            self.assertEqual(self.controller.snapshot().state, 'review_ready')

    def test_changes_invalidate_approval(self) -> None:
        """New context clears dependent evidence and preserves the old ledger."""
        self.ready()
        self.controller.decide_qa(self.decision(qa=True))
        self.context()
        snap = self.controller.snapshot()
        self.assertEqual(snap.state, 'validated')
        self.assertIsNone(snap.plan)
        self.assertIsNone(snap.package)
        self.assertEqual(snap.results, [])
        with self.assertRaises(WorkflowError):
            self.controller.propose(self.planner)  # Same revision cannot be reused.
        with self.assertRaises(WorkflowError):
            self.controller.load_context('{}', actor='MFG-1',role='manufacturing',reason='Missing new source')
        self.assertEqual(self.controller.snapshot().state, 'context_pending')
        self.assertIsNone(self.controller.snapshot().context)

    def test_bounded_errors_and_evidence(self) -> None:
        """Malformed service output retries twice, logs failures, preserves state."""
        self.context()
        service = BrokenPlanner()
        with self.assertRaises(WorkflowError):
            self.controller.propose(service)
        self.assertEqual(service.calls, 2)
        self.assertEqual(self.controller.snapshot().state, 'validated')
        self.assertEqual([e['status'] for e in self.controller.events()][-2:], ['error', 'error'])

    def test_anomalies_cannot_be_approved_after_review(self) -> None:
        """QA gate rejects anomaly evidence even after package preparation."""
        self.ready(anomalies=True)
        with self.assertRaises(WorkflowError):
            self.controller.decide_qa(self.decision(qa=True))
        self.assertEqual(self.controller.snapshot().state, 'review_ready')
        self.controller.decide_qa(self.decision(qa=True, decision='request_resolution'))
        self.assertEqual(self.controller.snapshot().state, 'blocked')

    def test_reimport_invalidates_review(self) -> None:
        """New evidence requires a new package and final QA decision."""
        self.ready()
        self.controller.decide_qa(self.decision(qa=True))
        self.controller.receive_results(ResultBatch(results=self.adapter.load_lims()).model_dump_json())
        self.assertEqual(self.controller.snapshot().state, 'results_received')
        self.assertIsNone(self.controller.snapshot().package)
        with self.assertRaises(WorkflowError):
            self.controller.prepare_review(FixtureReviewer(1))
        self.controller.prepare_review(FixtureReviewer(2))
        self.assertEqual(self.controller.snapshot().package.revision, 2)

    def test_agent_cannot_change_requirements(self) -> None:
        """Well-formed fabricated obligations still fail controller checks."""
        self.context()
        original = self.planner.propose(load_context(self.adapter,resolved=True).model_dump_json())
        data = json.loads(original)
        data['requirements'][0]['threshold']['value'] = 100
        class ForgedPlanner:
            """Supply a structurally valid but ungrounded plan."""

            def propose(self, context_json: str) -> str:
                """Return the altered payload."""
                return json.dumps(data)
        with self.assertRaises(WorkflowError):
            self.controller.propose(ForgedPlanner())
        self.assertEqual(self.controller.snapshot().state, 'validated')


    def test_evaluation_reimport_replaces_evidence_and_retains_stable_findings(self) -> None:
        """Identical imports never append results/findings and still invalidate old reviews."""
        self.ready(anomalies=True)
        # Review readiness must be invalidated by a new import before producing a package.
        payload = ResultBatch(results=self.adapter.load_lims()).model_dump_json()
        self.controller.receive_results(payload)
        first = self.controller.prepare_review(DeterministicReviewer(2)).package
        self.assertEqual(first.counts.received, 7)
        self.assertEqual(len(first.findings), 5)
        self.assertEqual(self.controller.snapshot().state, 'review_ready')
        with self.assertRaises(WorkflowError):
            self.controller.decide_qa(self.decision(qa=True))
        stale_decision = self.decision(qa=True)
        self.controller.receive_results(payload)
        self.assertIsNone(self.controller.snapshot().package)
        with self.assertRaises(WorkflowError):
            self.controller.decide_qa(stale_decision)
        second = self.controller.prepare_review(DeterministicReviewer(3)).package
        self.assertEqual(second.results, first.results)
        self.assertEqual(second.findings, first.findings)
        self.assertEqual(second.counts, first.counts)
        self.assertEqual(len(second.results), 7)
        reopened = Controller(self.path).snapshot()
        self.assertEqual(reopened.package, second)
        self.assertEqual(reopened.state, 'review_ready')

    def test_deterministic_normal_review_requires_explicit_qa_decision(self) -> None:
        """A clean evaluator output prepares evidence and cannot approve a run."""
        self.context()
        self.controller.propose(self.planner)
        with self.assertRaises(WorkflowError):
            self.controller.receive_results(ResultBatch(results=self.adapter.load_lims(anomalies=False)).model_dump_json())
        self.controller.decide_plan(self.decision())
        self.controller.collect(actor='TECH-1', role='manufacturing')
        self.controller.receive_results(ResultBatch(results=self.adapter.load_lims(anomalies=False)).model_dump_json())
        snap = self.controller.prepare_review(DeterministicReviewer(1))
        self.assertEqual(snap.state, 'review_ready')
        self.assertEqual(snap.package.counts.matched, 6)
        self.assertEqual(snap.package.findings, [])
        self.controller.decide_qa(self.decision(qa=True))
        self.assertEqual(self.controller.snapshot().state, 'qa_approved')


if __name__ == '__main__':
    unittest.main()
