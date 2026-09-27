"""Controller-owned QA package enrichment, independent of any draft producer."""
from cleanroom_os.contracts import (
    HumanPlanningDecision, HumanQADecision, PlanningContext, QAReviewPackage,
    SourceReference, parse_contract,
)
from cleanroom_os.evaluation import evaluate_results


def enrich_package(package: QAReviewPackage, context: PlanningContext, *, collected: bool,
                   prior_decisions: list[HumanPlanningDecision | HumanQADecision]) -> QAReviewPackage:
    """Attach exact context/history and canonical findings; never accept a producer's omissions."""
    evaluation = evaluate_results(package.plan, package.results)
    actions = []
    if package.plan.conflicts:
        actions.append('Manufacturing must resolve the cited planning conflicts through a source update and new plan revision.')
    if not collected:
        actions.append('Manufacturing must explicitly approve a complete plan and record simulated collection before QA approval.')
    if evaluation.counts.missing:
        actions.append('Supply the missing expected LIMS results; extra or duplicate records cannot substitute for them.')
    if any(f.kind in {'unmatched', 'duplicate', 'mismatch', 'out_of_limit', 'unknown'} for f in evaluation.findings):
        actions.append('Resolve the cited result/threshold findings using corrected source evidence and prepare a new package.')
    complete = collected and not evaluation.findings and not package.plan.conflicts
    c = evaluation.counts
    summary = (f'Plan {package.plan.plan_id} revision {package.plan.revision}: '
               f'{c.expected} expected, {c.received} received, {c.matched} context-matched, '
               f'{c.missing} missing, {c.blocked} blocked samples; '
               f'{c.unknown_count_requirements} obligations with unknown counts; '
               f'{len(evaluation.findings)} findings. '
               f'Collection {"simulated" if collected else "not recorded"}. '
               f'{"Complete evidence awaiting human QA decision" if complete else "Incomplete: QA may review, reject or request resolution; approval is blocked"}. '
               f'{len(prior_decisions)} prior human decisions retained for this run.')
    refs = {}

    def walk(value):
        if isinstance(value, dict):
            if set(value) == {'kind', 'source_id', 'version', 'locator'}:
                ref = SourceReference.model_validate(value)
                refs[(ref.kind, ref.source_id, ref.version, ref.locator)] = ref
            else:
                for child in value.values(): walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    for item in [context, evaluation, *prior_decisions]: walk(item.model_dump())
    data = package.model_dump()
    data.update(context=context.model_dump(), prior_decisions=[d.model_dump() for d in prior_decisions],
                collection_status='simulated' if collected else 'not_collected',
                completeness='complete' if complete else 'incomplete', summary=summary, required_actions=actions,
                counts=evaluation.counts.model_dump(), findings=[f.model_dump() for f in evaluation.findings],
                sources=[refs[key].model_dump() for key in sorted(refs)])
    return parse_contract(QAReviewPackage, QAReviewPackage.model_validate(data).model_dump_json())
