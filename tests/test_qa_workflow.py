"""Versioned QA packages, transactional local notifications and human decision gates."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cleanroom_os.adapters import FileInputAdapter, load_context
from cleanroom_os.contracts import QAReviewPackage, parse_contract
from cleanroom_os.controller import Controller, ResultBatch, Snapshot, WorkflowError
from cleanroom_os.evaluation import DeterministicReviewer
from cleanroom_os.mock_services import FixturePlanner
from cleanroom_os.notifications import LocalQAQueue

ROOT=Path(__file__).resolve().parents[1]/'fixtures/mock-facility'


class QAWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'run.sqlite'
        self.controller=Controller(self.path)
        self.adapter=FileInputAdapter(ROOT)

    def plan(self, resolved=True):
        context=load_context(self.adapter,resolved=resolved)
        self.controller.load_context(context.model_dump_json(),actor='MFG',role='manufacturing',reason='Synthetic source selection')
        return self.controller.propose(FixturePlanner(ROOT)).plan

    def decision(self, *, qa=False, decision=None, **changes):
        s=self.controller.snapshot()
        data=dict(decision_id=f'DECISION-{len(self.controller.events())}',plan_id=s.plan.plan_id,plan_revision=s.plan.revision,
                  actor_id='QA' if qa else 'MFG',role='qa' if qa else 'manufacturing',
                  decision=decision or ('approve' if qa else 'allow'),rationale='Review exact synthetic evidence',
                  decided_at='2026-09-27T17:00:00-04:00',sources=[dict(kind='human',source_id='HUMAN-DEMO',version='1',locator='explicit-decision')])
        if qa:data.update(package_id=s.package.package_id,package_revision=s.package.revision)
        return json.dumps(data|changes)

    def collected(self, anomalies=False):
        self.plan()
        self.controller.decide_plan(self.decision())
        self.controller.collect(actor='MFG',role='manufacturing')
        self.controller.receive_results(ResultBatch(results=self.adapter.load_lims(anomalies=anomalies)).model_dump_json())

    def review(self, revision=1):
        return self.controller.prepare_review(DeterministicReviewer(revision)).package

    def test_complete_package_traces_sources_history_and_sequence(self):
        self.collected()
        package=self.review()
        self.assertEqual(package.context,load_context(self.adapter,resolved=True))
        self.assertEqual(package.plan.requirements,package.context.recipe.requirements)
        self.assertEqual(package.collection_status,'simulated')
        self.assertEqual(package.completeness,'complete')
        self.assertEqual(package.counts.matched,6)
        self.assertIn('6 expected',package.summary)
        self.assertEqual(package.required_actions,[])
        self.assertEqual(len(package.prior_decisions),1)
        self.assertEqual(package.prior_decisions[0].decision,'allow')
        self.assertEqual(package.prior_decisions[0].plan_revision,package.plan.revision)
        for source in package.sources:
            if source.kind!='human': self.assertIsNotNone(self.adapter.resolve_source(source))
        self.assertTrue({'sop','recipe','schedule','lims','human'} <= {s.kind for s in package.sources})
        self.assertEqual(package,self.controller.package(package.package_id,package.revision))

    def test_blocked_plan_can_be_reviewed_but_not_approved_or_collected(self):
        self.plan(False)
        package=self.review()
        self.assertEqual(package.completeness,'incomplete')
        self.assertEqual(package.collection_status,'not_collected')
        self.assertEqual(package.counts.blocked,2)
        self.assertTrue(package.required_actions)
        self.assertEqual({f.kind for f in package.findings},{'missing','blocked'})
        self.assertEqual(self.controller.notifications()[0].completeness,'incomplete')
        for action in [lambda:self.controller.decide_qa(self.decision(qa=True)),
                       lambda:self.controller.decide_plan(self.decision()),
                       lambda:self.controller.collect(actor='MFG',role='manufacturing'),
                       lambda:self.controller.receive_results('{"results":[]}')]:
            with self.assertRaises(WorkflowError):action()
        self.assertEqual(self.controller.snapshot().state,'review_ready')
        self.controller.decide_qa(self.decision(qa=True,decision='request_resolution'))
        self.assertEqual(self.controller.snapshot().state,'blocked')
        self.assertIn('Manufacturing',self.controller.snapshot().action_required)

    def test_incomplete_preview_preserves_plan_and_collection_gates(self):
        self.plan()
        self.review()
        self.controller.decide_plan(self.decision())
        self.assertEqual(self.controller.snapshot().state,'plan_approved')
        self.assertIsNone(self.controller.snapshot().package)
        self.assertEqual(self.controller.notifications()[0].status,'superseded')
        self.review(2)
        self.controller.collect(actor='MFG',role='manufacturing')
        self.assertEqual(self.controller.snapshot().state,'collection_simulated')
        self.assertIsNone(self.controller.snapshot().package)
        self.assertTrue(all(n.status=='superseded' for n in self.controller.notifications()))
        package=self.review(3)
        self.assertEqual(package.collection_status,'simulated')
        self.assertEqual(package.counts.missing,6)
        with self.assertRaises(WorkflowError):self.controller.decide_qa(self.decision(qa=True))

    def test_notification_exact_target_persistence_and_idempotent_retry(self):
        self.collected();package=self.review()
        notification=self.controller.notifications()[0]
        self.assertEqual((notification.package_id,notification.package_revision),(package.package_id,package.revision))
        self.assertEqual(notification.plan_revision,package.plan.revision)
        self.assertIn(f'/revisions/{package.revision}',notification.target)
        self.controller.notify_qa();self.controller.notify_qa()
        self.controller=Controller(self.path)
        self.assertEqual(self.controller.notifications(),[notification])
        self.assertEqual(self.controller.package(package.package_id,package.revision),package)
        with self.assertRaises(WorkflowError):self.controller.package(package.package_id,99)

    def test_queue_failure_rolls_back_package_and_revision_but_logs_failure(self):
        self.collected()
        enqueue=LocalQAQueue.enqueue
        def fail_after_insert(db,package):
            enqueue(db,package)
            raise RuntimeError('Synthetic local queue failure')
        with patch.object(LocalQAQueue,'enqueue',side_effect=fail_after_insert):
            with self.assertRaises(WorkflowError):self.review()
        self.assertEqual(self.controller.notifications(),[])
        self.assertIsNone(self.controller.snapshot().package)
        self.assertEqual(self.controller.snapshot().last_package_revision,0)
        self.assertEqual(self.controller.events()[-1]['status'],'error')
        with self.assertRaises(WorkflowError):self.controller.package('QA-PACKAGE',1)
        self.review()
        self.assertEqual(len(self.controller.notifications()),1)

    def test_manufacturing_cannot_approve_reject_or_request_qa_resolution(self):
        self.collected();self.review()
        for decision in ['approve','reject','request_resolution']:
            with self.subTest(decision=decision),self.assertRaises(WorkflowError):
                self.controller.decide_qa(self.decision(qa=True,decision=decision,role='manufacturing'))
        self.assertEqual(self.controller.snapshot().state,'review_ready')
        self.assertEqual(self.controller.notifications()[0].status,'awaiting_qa')

    def test_rejection_and_resolution_are_actionable_and_need_new_evidence(self):
        self.collected(anomalies=True);first=self.review()
        self.controller.decide_qa(self.decision(qa=True,decision='request_resolution',rationale='Correct missing result'))
        self.assertEqual(self.controller.notifications()[0].status,'resolution_requested')
        self.assertIn('Correct missing result',self.controller.snapshot().action_required)
        with self.assertRaises(WorkflowError):self.review(2)
        with self.assertRaises(WorkflowError):self.controller.decide_plan(self.decision(decision='disallow'))
        self.controller.receive_results(ResultBatch(results=self.adapter.load_lims(anomalies=False)).model_dump_json())
        second=self.review(2)
        self.assertEqual(second.completeness,'complete')
        self.assertTrue(any(d.decision=='request_resolution' for d in second.prior_decisions))
        self.controller.decide_qa(self.decision(qa=True,decision='reject',rationale='QA requires corrected source evidence'))
        self.assertEqual(self.controller.snapshot().state,'qa_rejected')
        self.assertIn('QA requires corrected source evidence',self.controller.snapshot().action_required)
        self.assertEqual(self.controller.notifications()[-1].status,'rejected')
        self.controller.notify_qa()
        self.assertEqual(self.controller.notifications()[-1].status,'rejected')
        self.assertEqual(first,self.controller.package(first.package_id,first.revision))

    def test_evidence_change_supersedes_approval_and_rejects_stale_decision(self):
        self.collected();first=self.review()
        stale=self.decision(qa=True)
        self.controller.decide_qa(stale)
        self.assertEqual(self.controller.notifications()[0].status,'approved')
        self.controller.receive_results(ResultBatch(results=self.adapter.load_lims()).model_dump_json())
        self.assertEqual(self.controller.notifications()[0].status,'superseded')
        self.assertIsNone(self.controller.snapshot().package)
        second=self.review(2)
        with self.assertRaises(WorkflowError):self.controller.decide_qa(stale)
        with self.assertRaises(WorkflowError):self.controller.decide_qa(self.decision(qa=True))
        self.assertTrue(any(d.decision=='approve' and d.package_revision==first.revision for d in second.prior_decisions))
        self.assertEqual(self.controller.notifications()[-1].status,'awaiting_qa')

    def test_context_change_supersedes_and_keeps_historical_package(self):
        self.collected();package=self.review()
        self.controller.decide_qa(self.decision(qa=True))
        self.controller.load_context(load_context(self.adapter).model_dump_json(),actor='MFG',role='manufacturing',reason='Changed availability')
        self.assertFalse(self.controller.snapshot().collection_simulated)
        self.assertEqual(self.controller.notifications()[0].status,'superseded')
        self.assertEqual(package,self.controller.package(package.package_id,package.revision))

    def test_invalid_context_also_invalidates_notification(self):
        self.collected();self.review()
        with self.assertRaises(WorkflowError):self.controller.load_context('{}',actor='MFG',role='manufacturing',reason='Invalid source change')
        self.assertEqual(self.controller.snapshot().state,'context_pending')
        self.assertEqual(self.controller.notifications()[0].status,'superseded')

    def test_history_excludes_failed_decisions_and_current_decision_is_separate(self):
        self.collected()
        with self.assertRaises(WorkflowError):self.controller.decide_plan(self.decision(decision='allow',plan_revision=99))
        package=self.review()
        self.assertEqual(len(package.prior_decisions),1)
        self.controller.decide_qa(self.decision(qa=True))
        self.assertEqual(package,self.controller.package(package.package_id,package.revision))
        accepted=[e['action'] for e in self.controller.events() if e['status']=='ok']
        self.assertLess(accepted.index('decide_plan'),accepted.index('collect_simulated'))
        self.assertLess(accepted.index('collect_simulated'),accepted.index('receive_results'))
        self.assertLess(accepted.index('receive_results'),accepted.index('decide_qa'))

    def test_archive_revision_is_immutable(self):
        self.collected();package=self.review()
        altered=package.model_copy(update={'summary':'Changed after notification'})
        with self.controller._connect() as db:
            with self.assertRaisesRegex(ValueError,'overwritten'):LocalQAQueue.enqueue(db,altered)
        self.assertEqual(package,self.controller.package(package.package_id,package.revision))

    def test_legacy_collected_snapshots_keep_enforced_collection_history(self):
        self.collected()
        data=self.controller.snapshot().model_dump()
        data.pop('collection_simulated');data.pop('review_origin')
        self.assertTrue(Snapshot.model_validate(data).collection_simulated)
        data['state']='plan_approved'
        self.assertFalse(Snapshot.model_validate(data).collection_simulated)


if __name__=='__main__':unittest.main()
