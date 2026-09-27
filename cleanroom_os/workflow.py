"""Stepwise controller with offline or bounded agents and unauthenticated demo roles."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from cleanroom_os.adapters import FileInputAdapter, load_context
from cleanroom_os.controller import Controller, ResultBatch, WorkflowError
from cleanroom_os.mock_services import FixturePlanner
from cleanroom_os.evaluation import DeterministicReviewer
from cleanroom_os.planning import DeterministicPlanner
from cleanroom_os.agents import RequirementsAgent, PlanningAgent, ResultsReviewAgent
from cleanroom_os.opencode import OpenCodeTransport


def main() -> None:
    """Run exactly one explicit action against a persistent synthetic run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status','events','notifications','package','notify-qa','context','propose','allow','disallow','collect','results','review','qa-approve','qa-reject','qa-resolve'])
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--mode', choices=['offline', 'opencode'], default='offline')
    parser.add_argument('--model', help='Explicit provider/model; otherwise OPENCODE_MODEL')
    parser.add_argument('--agent-timeout', type=int, default=120)
    parser.add_argument('--fixtures', type=Path, default=Path('fixtures/mock-facility'))
    parser.add_argument('--resolved', action='store_true')
    parser.add_argument('--fixture-plan', action='store_true', help='Use hand-authored plan for fixture LIMS replay only')
    parser.add_argument('--normal', action='store_true')
    parser.add_argument('--actor', default='demo-user')
    parser.add_argument('--role', choices=['manufacturing','qa'], default='manufacturing')
    parser.add_argument('--reason', default='Explicit synthetic demo action')
    parser.add_argument('--revision', type=int, help='Exact displayed plan revision for a human decision')
    parser.add_argument('--package-id', help='Exact archived package ID to inspect')
    parser.add_argument('--package-revision', type=int, help='Exact displayed package revision for QA')
    args = parser.parse_args()
    controller = Controller(args.db)
    adapter = FileInputAdapter(args.fixtures)
    try:
        if args.mode == 'opencode' and args.fixture_plan:
            raise WorkflowError('--fixture-plan is only available in offline mode')
        transport = OpenCodeTransport(model=args.model, timeout=args.agent_timeout) if args.mode == 'opencode' else None
        if args.action == 'status':
            print(controller.snapshot().model_dump_json(indent=2))
            return
        if args.action == 'events':
            print(json.dumps(controller.events(), indent=2))
            return
        if args.action == 'notifications':
            print(json.dumps([n.model_dump(mode='json') for n in controller.notifications()], indent=2))
            return
        if args.action == 'package':
            if args.package_id is None or args.package_revision is None:
                raise WorkflowError('Specify --package-id and --package-revision from the QA notification')
            print(controller.package(args.package_id, args.package_revision).model_dump_json(indent=2))
            return
        if args.action == 'notify-qa':
            controller.notify_qa()
        elif args.action == 'context':
            controller.load_context_from(lambda: load_context(adapter,resolved=args.resolved).model_dump_json(), actor=args.actor,role=args.role,reason=args.reason,
                                         requirements_service=RequirementsAgent(transport) if transport else None,
                                         attempts=2 if transport else 1)
        elif args.action == 'propose':
            revision = controller.snapshot().last_revision + 1
            service = PlanningAgent(transport, revision) if transport else (
                FixturePlanner(args.fixtures) if args.fixture_plan else DeterministicPlanner(revision))
            controller.propose(service)
        elif args.action == 'collect':
            controller.collect(actor=args.actor,role=args.role)
        elif args.action == 'results':
            controller.receive_results(ResultBatch(results=adapter.load_lims(anomalies=not args.normal)).model_dump_json())
        elif args.action == 'review':
            revision = controller.snapshot().last_package_revision + 1
            controller.prepare_review(ResultsReviewAgent(transport, revision) if transport else DeterministicReviewer(revision))
        else:
            snapshot = controller.snapshot()
            if snapshot.plan is None or args.revision is None:
                raise WorkflowError('A current plan and explicit --revision are required')
            decision = dict(decision_id=str(uuid4()), plan_id=snapshot.plan.plan_id,plan_revision=args.revision,
                            actor_id=args.actor,role=args.role,rationale=args.reason,decided_at=datetime.now(timezone.utc).isoformat(),
                            sources=[dict(kind='human',source_id=args.actor,version='1',locator='explicit-cli-decision')])
            if args.action in {'allow','disallow'}:
                decision['decision'] = args.action
                controller.decide_plan(json.dumps(decision))
            else:
                if snapshot.package is None or args.package_revision is None:
                    raise WorkflowError('A current package and explicit --package-revision are required')
                decision.update(package_id=snapshot.package.package_id,package_revision=args.package_revision,
                                decision={'qa-approve':'approve','qa-reject':'reject','qa-resolve':'request_resolution'}[args.action])
                controller.decide_qa(json.dumps(decision))
        print(controller.snapshot().model_dump_json(indent=2))
    except (WorkflowError, ValueError, OSError) as exc:
        parser.exit(1, f'{type(exc).__name__}: {exc}\n')


if __name__ == '__main__':
    main()
