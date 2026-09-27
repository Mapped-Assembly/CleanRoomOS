# Deterministic sampling planning

`cleanroom_os.planning.generate_plan(context, plan_id=..., revision=...)`
creates a proposal from validated inputs. `DeterministicPlanner` implements the
controller's JSON proposal-service protocol. No model or fixture plan is read.
The offline workflow CLI uses it by default:

```bash
python -m cleanroom_os.workflow context --db run.sqlite
python -m cleanroom_os.workflow propose --db run.sqlite
# Inspect the blocked proposal, then explicitly supply the human replacement:
python -m cleanroom_os.workflow context --db run.sqlite --resolved --reason "Manufacturing supplied replacement schedule"
python -m cleanroom_os.workflow propose --db run.sqlite
```

The controller persists a new revision, clears earlier approvals, and retains prior
snapshots in its event ledger. Updating B does not resolve a separate C conflict.
Blocked plans cannot be approved. Role attribution remains an unauthenticated demo.

## Inputs and scheduling policy

`PlanningContext.execution` carries the facility's explicit single-technician
availability, starting room, sample duration, setup duration and travel duration.
Missing execution data and explicitly `unknown` durations/resource windows generate
conflicts; nothing is inferred from the size of an access window. Zero setup/travel
is accepted only when explicitly supplied. Positive sample duration is required.

The baseline intersects access, shift and technician windows, subtracts occupancy,
then places complete requirement batches at the earliest feasible start. Ties use
window deadline then requirement ID. Setup must fit inside room access, and travel
must fit after the previous visit. A gap between samples is treated as a new visit
requiring setup. All work uses the supplied sequential collection mode.

This is a conservative greedy baseline, not a global optimizer. An insufficient-window
conflict means the batch cannot fit in the selected sequence, not that every possible
sequence is impossible. A human can supply revised constraints or another producer
can propose a different valid order. No workaround, parallel crew, new access window,
threshold change, or count reduction is assumed.

Conflicts contain stable IDs, a machine-readable `code`, requirement/room identity,
reason and source references. Required counts/types/thresholds remain exactly in
`plan.requirements`, including blocked rooms. Unknown counts/thresholds stay blocked.
Contradictory SOP/recipe inputs are rejected before planning as structured
`ContractValidationError.issues`; they cannot become a validated planning context.

Sample IDs hash recipe identity, requirement identity and ordinal. Schedule edits and
plan revisions preserve those IDs. Plan revisions are caller-supplied; the CLI uses
the controller's persisted last revision plus one. Samples cite requirement,
schedule, occupancy and execution evidence and explain the sequencing policy.

## Independent validation

`validate_plan(context, plan)` reparses both contracts and rejects altered/missing
obligations, overlapping or unordered visits, inaccessible or occupied setup/collection,
wrong sample durations, insufficient travel, resource-bound violations and missing
current scheduling/execution evidence. It does not call the planner or require its
particular order or IDs. The controller applies this same validator to every producer.
Unresolved conflicts remain visible and block approval.

The original fixture plans/results remain immutable oracle examples. Their hand-authored
sample IDs differ from generated IDs. For replaying those exact LIMS fixtures use
`propose --fixture-plan`, as shown in the controller demo. Generated plans need results
referencing their actual sample IDs and revision; mismatched fixture results cannot pass QA.

Run all offline acceptance/regression tests with:

```bash
python -m unittest discover -s tests -v
```
