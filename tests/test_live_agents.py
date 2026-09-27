"""Opt-in real OpenCode/model acceptance; the database and observations are synthetic."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from cleanroom_os.adapters import FileInputAdapter, load_context
from cleanroom_os.agents import RequirementsAgent, PlanningAgent, ResultsReviewAgent
from cleanroom_os.contracts import ResultBatch
from cleanroom_os.controller import Controller
from cleanroom_os.opencode import OpenCodeTransport
from test_agents import synthetic_results

ROOT=Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('CLEANROOM_LIVE_OPENCODE')=='1', 'Requires explicit live OpenCode/model configuration')
class LiveAgentTests(unittest.TestCase):
    def test_three_roles_through_controller(self):
        """A failed model/contract fails the test, with no fallback or fake live response."""
        with tempfile.TemporaryDirectory() as tmp:
            controller=Controller(Path(tmp)/'synthetic-live.sqlite')
            transport=OpenCodeTransport(directory=ROOT, timeout=120)
            context=load_context(FileInputAdapter(ROOT/'fixtures/mock-facility'),resolved=True)
            controller.load_context_from(lambda:context.model_dump_json(), actor='LIVE-TEST-MFG',role='manufacturing',
                reason='Explicit synthetic integration test',requirements_service=RequirementsAgent(transport),attempts=2)
            sessions=[transport.last_session_id]
            plan=controller.propose(PlanningAgent(transport,1)).plan
            sessions.append(transport.last_session_id)
            self.assertEqual(plan.conflicts,[])
            controller.decide_plan(json.dumps(dict(decision_id='LIVE-TEST-ALLOW',plan_id=plan.plan_id,plan_revision=plan.revision,
                actor_id='LIVE-TEST-MFG',role='manufacturing',decision='allow',rationale='Synthetic integration test only',
                decided_at='2026-09-27T09:00:00-04:00',sources=[dict(kind='human',source_id='LIVE-TEST-MFG',version='1',locator='test-decision')])) )
            controller.collect(actor='LIVE-TEST-MFG',role='manufacturing')
            controller.receive_results(ResultBatch(results=synthetic_results(plan)).model_dump_json())
            snapshot=controller.prepare_review(ResultsReviewAgent(transport,1))
            sessions.append(transport.last_session_id)
            self.assertEqual(len(set(sessions)),3)
            self.assertEqual(snapshot.state,'review_ready')
            self.assertEqual(snapshot.package.counts.matched,6)
            self.assertEqual(snapshot.package.findings,[])
            self.assertTrue(snapshot.package.agent_explanation)
            self.assertTrue(all(e['status']=='ok' for e in controller.events()))


if __name__=='__main__':unittest.main()
