"""Controller-owned role boundaries with deliberate hostile/malformed replies."""
import json
from pathlib import Path
import tempfile
import unittest

from cleanroom_os.adapters import FileInputAdapter, load_context
from cleanroom_os.agent_contracts import AgentResponse, RequirementsDraft
from cleanroom_os.agents import RequirementsAgent, PlanningAgent, ResultsReviewAgent
from cleanroom_os.contracts import LIMSResult, QAReviewPackage, ResultBatch, SamplingPlan, parse_contract
from cleanroom_os.controller import Controller, WorkflowError
from cleanroom_os.evaluation import DeterministicReviewer

ROOT = Path(__file__).resolve().parents[1]


class MockTransport:
    """Returns explicit deterministic role responses; never silently selected for live failures."""
    def __init__(self, edit=None):
        self.roles = []
        self.edit = edit

    def complete(self, role, payload, schema):
        self.roles.append(role)
        if role == 'cleanroom-requirements':
            output = payload['source_documents']
        elif role == 'cleanroom-planning':
            output = {key: payload['baseline'][key] for key in ['samples', 'conflicts']}
        else:
            evaluation = payload['evaluation']
            output = dict(summary=dict(text='Synthetic evidence awaits human QA.', sources=evaluation['sources']),
                          explanations=[dict(finding_id=f['finding_id'], text=f['description'], sources=f['sources'])
                                        for f in evaluation['findings']])
        response = dict(output=output, assumptions=[], gaps=payload['explicit_gaps'])
        if self.edit:
            self.edit(role, response)
        return json.dumps(response)


def synthetic_results(plan):
    """New synthetic readings reference the actual plan, never relabel old LIMS records."""
    return [LIMSResult(result_id=f'MOCK-{i}', plan_id=plan.plan_id, plan_revision=plan.revision,
                      sample_id=s.sample_id, room_id=s.room_id, sample_type=s.sample_type,
                      collected_at=s.window.end, value=3.0, unit='CFU/m3',
                      sources=[dict(kind='lims', source_id='MOCK-LIMS', version='1', locator=f'/results/{i}')])
            for i, s in enumerate(plan.samples)]


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.controller = Controller(Path(self.temp.name)/'run.sqlite')
        self.context = load_context(FileInputAdapter(ROOT/'fixtures/mock-facility'), resolved=True)
        self.transport = MockTransport()

    def load(self, transport=None):
        return self.controller.load_context_from(lambda: self.context.model_dump_json(),
            actor='MFG-TEST', role='manufacturing', reason='Synthetic source import',
            requirements_service=RequirementsAgent(transport or self.transport), attempts=2)

    def collect(self):
        plan = self.controller.snapshot().plan
        self.controller.decide_plan(json.dumps(dict(decision_id='TEST-ALLOW', plan_id=plan.plan_id,
            plan_revision=plan.revision, actor_id='MFG-TEST', role='manufacturing', decision='allow',
            rationale='Explicit synthetic test decision', decided_at='2026-09-27T09:00:00-04:00',
            sources=[dict(kind='human',source_id='MFG-TEST',version='1',locator='test-decision')])))
        self.controller.collect(actor='MFG-TEST',role='manufacturing')
        self.controller.receive_results(ResultBatch(results=synthetic_results(plan)).model_dump_json())

    def test_all_three_roles_through_controller_owned_transitions(self):
        self.load()
        self.controller.propose(PlanningAgent(self.transport, 1))
        self.collect()
        snapshot = self.controller.prepare_review(ResultsReviewAgent(self.transport, 1))
        self.assertEqual(self.transport.roles, ['cleanroom-requirements','cleanroom-planning','cleanroom-results-review'])
        self.assertEqual(snapshot.state, 'review_ready')
        self.assertEqual(snapshot.package.counts.matched, 6)
        self.assertEqual(snapshot.package.findings, [])
        self.assertIn('awaits human QA', snapshot.package.agent_explanation)
        self.assertIsNotNone(snapshot.requirements_assessment)

    def test_direct_adapter_invocation_cannot_advance_database(self):
        before = self.controller.snapshot().model_dump_json()
        RequirementsAgent(self.transport).extract(self.context.model_dump_json())
        plan = PlanningAgent(self.transport, 1).propose(self.context.model_dump_json())
        ResultsReviewAgent(self.transport, 1).review(plan, '{"results":[]}')
        self.assertEqual(before, self.controller.snapshot().model_dump_json())
        self.assertEqual(self.controller.events(), [])

    def test_fabricated_count_threshold_and_source_rejected(self):
        for field in ['count', 'threshold', 'sources']:
            def edit(role, response):
                r=response['output']['recipe']['requirements'][0]
                if field == 'count': r['count']=99
                elif field == 'threshold': r['threshold']['value']=999
                else: r['sources'][0]['locator']='/invented'
            with self.subTest(field=field), self.assertRaises(WorkflowError):
                self.load(MockTransport(edit))
            self.assertEqual(self.controller.snapshot().state, 'context_pending')
            self.assertIsNone(self.controller.snapshot().context)

    def test_fabricated_windows_and_requirement_fields_rejected(self):
        self.load()
        def invalid_window(role, response):
            response['output']['samples'][0]['window'].update(start='2026-09-27T08:00:00-04:00',end='2026-09-27T08:05:00-04:00')
        def changed_requirements(role, response):
            response['output']['requirements']=[]
        for edit in [invalid_window, changed_requirements]:
            transport = MockTransport(edit)
            with self.subTest(edit=edit.__name__), self.assertRaises(WorkflowError):
                self.controller.propose(PlanningAgent(transport,1))
            self.assertEqual(len(transport.roles),2)
            self.assertEqual(self.controller.snapshot().state,'validated')
            self.assertIsNone(self.controller.snapshot().plan)

    def test_assumptions_and_omitted_unknown_gaps_rejected(self):
        def assumption(role,response):
            response['assumptions']=[dict(status='unknown',reason='Assume extra technician',sources=self.context.recipe.requirements[0].sources[0:1])]
            response['assumptions'][0]['sources']=[s.model_dump() for s in response['assumptions'][0]['sources']]
        with self.assertRaises(WorkflowError): self.load(MockTransport(assumption))
        data=self.context.model_dump(mode='json')
        for doc in ['sop','recipe']:
            data[doc]['requirements'][0]['count']=dict(status='unknown',reason='Count absent',sources=data['sop']['requirements'][0]['sources'])
        self.context=type(self.context).model_validate(data)
        with self.assertRaises(WorkflowError): self.load(MockTransport(lambda role,r:r.update(gaps=[])))
        self.load()
        self.assertTrue(self.controller.snapshot().requirements_assessment.gaps)

    def test_model_unavailable_has_bounded_retries_and_visible_errors(self):
        class Unavailable:
            calls=0
            def complete(self,*args):
                self.calls+=1
                raise ValueError('Model unavailable')
        transport=Unavailable()
        with self.assertRaises(WorkflowError): self.load(transport)
        self.assertEqual(transport.calls,2)
        events=self.controller.events()
        self.assertEqual(len(events),2)
        self.assertTrue(all(e['status']=='error' for e in events))
        self.assertIn('Model unavailable',events[-1]['details'])

    def test_review_cannot_omit_findings_or_invent_evidence_or_decision(self):
        self.load()
        self.controller.propose(PlanningAgent(self.transport,1))
        self.collect()
        self.controller.receive_results('{"results":[]}')
        def omit(role,r): r['output']['explanations']=[]
        def invent(role,r): r['output']['summary']['sources'][0]['locator']='/made-up'
        def approve(role,r): r['output']['decision']='approve'
        for edit in [omit,invent,approve]:
            transport=MockTransport(edit)
            with self.subTest(edit=edit.__name__),self.assertRaises(WorkflowError):
                self.controller.prepare_review(ResultsReviewAgent(transport,1))
            self.assertEqual(len(transport.roles),1)
            self.assertEqual(self.controller.snapshot().state,'results_received')

    def test_controller_rejects_forged_packages_from_any_service(self):
        self.load();self.controller.propose(PlanningAgent(self.transport,1));self.collect()
        self.controller.receive_results('{"results":[]}')
        class ForgedReviewer:
            def review(inner,plan,results):
                package=json.loads(DeterministicReviewer(1).review(plan,results))
                package['findings']=[]
                return json.dumps(package)
        with self.assertRaises(WorkflowError): self.controller.prepare_review(ForgedReviewer())
        self.assertIsNone(self.controller.snapshot().package)

    def test_active_config_has_no_autonomous_coordinator_or_tools(self):
        config=json.loads((ROOT/'opencode.json').read_text())
        self.assertEqual(config['permission'],{'*':'deny'})
        for name,agent in config['agent'].items():
            self.assertEqual(agent['permission'],{'*':'deny'})
            self.assertEqual(agent['steps'],1)
            self.assertNotIn(name,['cleanroom','cleanroom-operation','cleanroom-risk','cleanroom-compliance'])
        self.assertEqual({k for k,v in config['agent'].items() if v['mode']=='subagent'},
                         {'cleanroom-requirements','cleanroom-planning','cleanroom-results-review'})


if __name__ == '__main__': unittest.main()
