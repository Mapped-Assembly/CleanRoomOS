"""Offline service adapters for controller demonstrations, not production planning."""
from pathlib import Path

from cleanroom_os.adapters import Fixture
from cleanroom_os.contracts import PlanningContext, QAReviewPackage, SamplingPlan, parse_contract
from cleanroom_os.controller import ResultBatch


class FixturePlanner:
    """Return the hand-authored oracle selected by the supplied schedule version."""

    def __init__(self, directory: Path) -> None:
        """Bind to local synthetic fixtures."""
        self.directory = directory

    def propose(self, context_json: str) -> str:
        """Refuse unknown versions rather than inventing an oracle."""
        context = parse_contract(PlanningContext, context_json)
        if context.schedule.version not in {'1', '2'}:
            raise ValueError('No mock plan for this schedule version')
        filename = 'plan-blocked.json' if context.schedule.version == '1' else 'plan-resolved.json'
        return parse_contract(Fixture[SamplingPlan], (self.directory/filename).read_text()).data.model_dump_json()


class FixtureReviewer:
    """Wrap exact input evidence for demo review; no automatic anomaly classification."""

    def __init__(self, revision: int) -> None:
        """Use an explicitly supplied new package revision."""
        self.revision = revision

    def review(self, plan_json: str, results_json: str) -> str:
        """Keep all input evidence; final gates independently reject anomalies."""
        plan = parse_contract(SamplingPlan, plan_json)
        results = parse_contract(ResultBatch, results_json)
        return QAReviewPackage(package_id='SYN-PACKAGE-001', revision=self.revision,
                               plan=plan, results=results.results, findings=[],
                               sources=[source for r in plan.requirements for source in r.sources]).model_dump_json()
