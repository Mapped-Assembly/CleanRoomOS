# Structured sampling contracts

Issue #1 introduces `cleanroom_os.contracts`, a Python 3.11+ Pydantic v2 exchange layer. Install with `python -m pip install -e .`; run `python -m unittest discover -s tests -v`.

## Exchange boundary

```python
from cleanroom_os.contracts import ContractValidationError, SamplingPlan, parse_contract

# payload is the JSON string returned by an adapter or agent.
try:
    plan = parse_contract(SamplingPlan, payload)
except ContractValidationError as exc:
    errors = [issue.model_dump() for issue in exc.issues]
```

Every model exposes `model_json_schema()` for structured-output adapters and `model_dump_json()` for transport. Unknown fields and plain prose are rejected. Errors include field paths, codes, and messages; raw input payloads are excluded. Consumers must validate each complete payload at the boundary rather than mutate nested model collections in place.

## Model mapping

| Contract | Purpose |
|---|---|
| SourceReference | Source kind, stable ID, exact version, section or fixture-record locator |
| Unknown | Explicit missing information with reason and evidence |
| Threshold | Authoritative value, comparison, and unit |
| SamplingRequirement / SOPRequirements / Recipe | Required counts/types by room, versioned authority |
| Room / TimeWindow / Occupancy / Availability / DailySchedule | Room identity and known operational constraints |
| PlanningContext | Cross-source consistency and known-room validation |
| PlannedSample / SamplingPlan / PlanningConflict | Derived sequence, original obligations, and unresolved blockers |
| LIMSResult | Ingested measurement and source identity, even if unmatched |
| QAFinding / QAReviewPackage | Evidence-backed review inputs with optional reconciled counts for older package compatibility |
| ResultBatch / ResultEvaluation / EvaluationCounts | LIMS snapshot, exact-revision findings and separate sample/record counts |
| HumanPlanningDecision / HumanQADecision | Revision-specific operational and final QA decisions |
| WorkflowTransition | Requested state change with expected state and attribution |
| ValidationIssue / ContractValidationError | Structured failures for controllers/adapters |

## Authority and unknowns

SOP and recipe requirements carry references to their exact document versions. Schedule references cannot authorize new requirements or thresholds. `PlanningContext` rejects contradictory counts or threshold definitions for the same room/sample-type pair; it never selects a winner. This first contract assumes one sampling obligation per room/type within each source; represent separate sampling phases explicitly in a future contract revision rather than merging incompatible obligations.

Counts use positive strict integers. An unknown count or threshold must use `{ "status": "unknown", "reason": "...", "sources": [...] }`; null or omission is not a default. Unknown counts require a conflict and cannot produce planned samples. Unknown thresholds can remain in proposals but cannot be treated as passing evaluation. Supported units are an explicit POC vocabulary (`CFU`, `CFU/m3`, `CFU/plate`, `CFU/cm2`, `particles/m3`); extend deliberately for additional measurement types. No conversion or threshold is inferred.

Plans retain the original requirement list. Each scheduled sample cites its requirement and scheduling source. Missing sample counts require an explicit conflict; over-counts and identity changes fail. A blocked plan remains a valid *proposal*, not an executable or approved plan.

LIMS ingestion deliberately permits sample/plan mismatches so the deterministic evaluator can report them. Review packages reject ambiguous duplicate record IDs but may hold distinct records concerning the same sample. QA decision role is restricted to `qa`; role strings do not authenticate a human.

## Validation and workflow boundaries

These models validate structure and selected cross-record invariants. They do not perform scheduling, match LIMS results, check window feasibility against occupancy, enforce actual state transitions, authenticate roles, persist records, or execute approvals. The controller must compare derived requirements against the validated input context, check source locators against actual documents, validate the current revision and completeness, and authorize every transition. The [controller](controller.md), [planner](planning.md), and [evaluator](evaluation.md) implement the deterministic gates, feasibility checks, and result matching. The current OpenCode conversation simulation is unchanged.

Tests use small synthetic payload builders, not the full mock facility deliverable in issue #2.
