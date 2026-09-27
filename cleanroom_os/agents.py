"""Bounded data-only adapters for three roles; no adapter receives a controller."""
from typing import Protocol

from cleanroom_os.agent_contracts import (
    AgentResponse, RequirementsDraft, ReviewDraft, ScheduleDraft, source_gaps, validate_response,
)
from cleanroom_os.contracts import PlanningContext, QAReviewPackage, ResultBatch, SamplingPlan, parse_contract
from cleanroom_os.evaluation import evaluate_results
from cleanroom_os.planning import generate_plan, sample_id, validate_plan


class AgentTransport(Protocol):
    def complete(self, role: str, payload: dict, schema: dict) -> str:
        """Return one JSON-only reply or raise; retries belong to the controller."""
        ...


class OfflineRequirements:
    """Reproducible source extraction with explicit gaps and no inferred facts."""

    def extract(self, context_json: str) -> str:
        context = parse_contract(PlanningContext, context_json)
        draft = RequirementsDraft(sop=context.sop, recipe=context.recipe)
        return AgentResponse[RequirementsDraft](output=draft, assumptions=[], gaps=source_gaps(draft)).model_dump_json()


class RequirementsAgent:
    def __init__(self, transport: AgentTransport):
        self.transport = transport

    def extract(self, context_json: str) -> str:
        context = parse_contract(PlanningContext, context_json)
        draft = RequirementsDraft(sop=context.sop, recipe=context.recipe)
        schema = AgentResponse[RequirementsDraft].model_json_schema()
        return self.transport.complete('cleanroom-requirements',
            {'source_documents': draft.model_dump(mode='json'),
             'explicit_gaps': [g.model_dump(mode='json') for g in source_gaps(draft)]}, schema)


class PlanningAgent:
    def __init__(self, transport: AgentTransport, revision: int, plan_id='SAMPLING-PLAN'):
        self.transport, self.revision, self.plan_id = transport, revision, plan_id

    def propose(self, context_json: str) -> str:
        context = parse_contract(PlanningContext, context_json)
        baseline = generate_plan(context, plan_id=self.plan_id, revision=self.revision)
        gaps = source_gaps(context)
        payload = {'context': context.model_dump(mode='json'),
                   'baseline': baseline.model_dump(mode='json'),
                   'explicit_gaps': [g.model_dump(mode='json') for g in gaps]}
        response = parse_contract(AgentResponse[ScheduleDraft], self.transport.complete(
            'cleanroom-planning', payload, AgentResponse[ScheduleDraft].model_json_schema()))
        validate_response(response, gaps)
        plan = SamplingPlan(plan_id=self.plan_id, revision=self.revision,
                            requirements=context.recipe.requirements, **response.output.model_dump())
        validate_plan(context, plan)
        allowed_sources = [s for entry in [*baseline.samples, *baseline.conflicts, *baseline.requirements]
                           for s in entry.sources]
        if any(s not in allowed_sources for entry in [*plan.samples, *plan.conflicts] for s in entry.sources):
            raise ValueError('Planning Agent invented source evidence')
        for conflict in baseline.conflicts:
            if conflict.code != 'insufficient_window' and conflict not in plan.conflicts:
                raise ValueError('Planning Agent changed an unresolved source constraint')
        # The planned identity is fixed by requirement and ordinal, not model choice.
        for requirement in context.recipe.requirements:
            proposed = [s.sample_id for s in plan.samples if s.requirement_id == requirement.requirement_id]
            expected = {sample_id(context, requirement, i) for i in range(1, len(proposed) + 1)}
            if set(proposed) != expected:
                raise ValueError('Agent changed stable planned-sample identity')
        return plan.model_dump_json()


class ResultsReviewAgent:
    def __init__(self, transport: AgentTransport, revision: int, package_id='QA-PACKAGE'):
        self.transport, self.revision, self.package_id = transport, revision, package_id

    def review(self, plan_json: str, results_json: str) -> str:
        plan = parse_contract(SamplingPlan, plan_json)
        batch = parse_contract(ResultBatch, results_json)
        evaluation = evaluate_results(plan, batch.results)
        gaps = source_gaps(plan) + [g for g in source_gaps(batch) if g not in source_gaps(plan)]
        payload = {'plan': plan.model_dump(mode='json'), 'results': batch.model_dump(mode='json'),
                   'evaluation': evaluation.model_dump(mode='json'),
                   'explicit_gaps': [g.model_dump(mode='json') for g in gaps]}
        response = parse_contract(AgentResponse[ReviewDraft], self.transport.complete(
            'cleanroom-results-review', payload, AgentResponse[ReviewDraft].model_json_schema()))
        validate_response(response, gaps)
        draft = response.output
        expected = {f.finding_id: f for f in evaluation.findings}
        ids = [e.finding_id for e in draft.explanations]
        if len(ids) != len(set(ids)) or set(ids) != set(expected):
            raise ValueError('Agent must explain every finding exactly once')
        if any(s not in evaluation.sources for s in draft.summary.sources):
            raise ValueError('Agent summary invented source evidence')
        for explanation in draft.explanations:
            if any(s not in expected[explanation.finding_id].sources for s in explanation.sources):
                raise ValueError('Agent explanation invented source evidence')
        return QAReviewPackage(package_id=self.package_id, revision=self.revision,
            plan=plan, results=batch.results, counts=evaluation.counts, findings=evaluation.findings,
            sources=evaluation.sources, agent_explanation=draft.model_dump_json()).model_dump_json()
