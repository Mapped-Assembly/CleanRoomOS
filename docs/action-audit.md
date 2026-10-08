# Audited actions and operational policy

Issue #23 adds a persistent action lifecycle to the runtime from #22. Every
well-formed proposal for an existing instance is saved, including proposals with
missing actor/task/policy references. Execution requests, rejected requests,
operator interventions, and adapter outcomes append new provenance events.

## Run locally

In the installed Python environment, start an explicit simulation from PowerShell:

```powershell
$env:CLEANROOM_DATABASE_URL = "sqlite+pysqlite:///action-demo.sqlite"
$env:CLEANROOM_OPERATOR_TOKEN = "<choose-a-private-operator-token>"
$env:CLEANROOM_ACTION_EXECUTOR = "simulation"
cleanroom-server
```

The PostgreSQL URL in [persistent-runtime.md](persistent-runtime.md) remains the
production default. SQLite is for local development/tests. The new tables are
additive and initialized at startup; existing #22 records are retained.

Open `http://localhost:8000/docs` for the complete request/response schemas. Writes
to policies, SOP references, contamination observations, maintenance, calibration,
and interventions require `Authorization: Bearer <CLEANROOM_OPERATOR_TOKEN>`.
An unset token disables those writes with HTTP 503; missing/incorrect credentials
return 401. Keep this credential outside agent prompts and agent tool credentials.

This shared credential controls operator access; the supplied `operator_id` is
an audit attribution, not independently verified individual identity. Actor/task
creation, proposals, execution requests, and reads are intended for trusted local
clients. This is not a multi-tenant authentication implementation.

With the default `CLEANROOM_ACTION_EXECUTOR=disabled`, execution requests are
persisted and blocked. `simulation` explicitly returns `outputs.mode=simulation`
and does not actuate equipment. A future trusted device adapter implements
`ActionExecutor.execute(action_id, proposal)` and is injected into `create_app`.
The action ID is its external idempotency key. No built-in physical adapter exists.

## Prepare trusted context

Use `POST /v1/instances` to create an instance, then the following endpoints within
`/v1/instances/{instance_id}`:

1. `POST /records` with `kind=agent` and `kind=task`. The actor must be
   `ready`, `idle`, `active`, or `operational`; the task must be `queued`, `active`,
   `in_progress`, or `running`. A task assigned to a different actor is blocked.
2. `POST /policies` with an immutable revision, governing SOP revisions, allowed
   zones, and any narrowly permitted operator overrides:

   ```json
   {
     "policy_id": "sampling",
     "revision": 1,
     "sop_refs": [{"document_id": "SOP-014", "version": "2.1", "locator": "section 4.2"}],
     "allowed_zones": ["zone-a"],
     "require_maintenance": true,
     "require_calibration": true,
     "overridable_checks": ["risk_review"]
   }
   ```

3. Append `maintenance` and `calibration` records through `POST /records`.
   Both identify `equipment_id`. Maintenance supplies `status`, `performed_at`,
   `due_at`, and optional `faults`/`notes`. Calibration supplies `status`,
   `calibrated_at`, and `due_at`. Validity requires timezone-aware service times
   at or before the decision and an expiry strictly after the decision.
4. Append a `contamination` record identifying `zone_id`, a `status` of `clean`,
   `suspected`, `contaminated`, or `unknown`, `observed_at`, `expires_at`, and at
   least one evidence item. Each evidence item has `source`, `reference`, a
   timezone-aware `captured_at`, and optional JSON `details`. Its capture time
   must not follow the observation. Use current timestamps; expired observations
   cannot authorize an action.

The newest persisted observation/service record for that instance and zone or
equipment is used, ordered by server creation time and record ID. Full source
records are copied into each decision. Clients cannot supply a passing policy
result, equipment state, or a preferred old policy revision in an action proposal.
The highest installed policy revision is always evaluated; revisions cannot be
replaced or installed out of order. SOP content itself is not fetched or validated.

## Propose, execute, inspect

`POST /actions` accepts:

```json
{
  "actor_id": "robot-1",
  "task_id": "task-1",
  "name": "collect_surface_sample",
  "target_id": "surface-7",
  "equipment_id": "pipette-1",
  "zone_id": "zone-a",
  "policy_id": "sampling",
  "reason": "Scheduled sample required by SOP-014",
  "risk_level": "low",
  "inputs": {"sample_count": 1},
  "evidence": []
}
```

The response includes a server-generated ID, `state`, current `decision_id`, and
ordered events. A proposal becomes `ready`, `blocked`, or `escalated`.

`POST /actions/{action_id}/execute` accepts the proposal's `actor_id` and a
nonempty `reason`. The runtime rechecks current policy, evidence, and expiry at
execution time. A successful dispatch first commits an `execution_started` event
and sets `executing`, then calls the adapter outside the database transaction.
Its outcome appends `completed` or `failed`; a failure creates an incident.
Adapter exceptions are sanitized before they enter the audit/API response.

An action may be dispatched once. Requests while `executing` or after completion,
failure, rejection, or a blocking decision are recorded without invoking the
adapter. A blocked/escalated action needs a new proposal after remediation, or an
eligible operator intervention. A database commit failure before dispatch prevents
the adapter call. A process crash or failed outcome commit after dispatch leaves
`executing` durable and non-retryable; external reconciliation is required rather
than claiming an unknown hardware outcome succeeded.

Policy refusals are business outcomes in successful HTTP responses: inspect the
returned state and last event. HTTP 422 means the request shape was invalid, and
404 means the instance/action does not exist in the requested scope. These are
not accepted action proposals and do not create an action record.

## Gates and operator intervention

| Condition | Decision | Override |
|---|---|---|
| Missing policy/actor/task, invalid actor/task state, forbidden zone, high reported risk | Block | Never |
| Detected contamination, equipment fault/out of service, required overdue maintenance or expired calibration | Block | Never |
| Missing, unknown, stale, or future-dated contamination/service evidence; service marked due | Escalate | Never |
| Medium reported risk (`risk_review`) | Escalate | Only if the policy explicitly permits this check |
| Fresh suspected contamination (`contamination_suspected`) | Escalate | Only if the policy explicitly permits this check |
| Nominal risk, allowed zone, current clean observation, valid required equipment state | Allow | Not needed |

All checks are retained; a hard block wins over an escalation. The gate ruleset
version is stored alongside the policy/SOP revisions. `require_maintenance` and
`require_calibration` default to true and can only be changed through a new trusted
policy revision. Known faults/out-of-service equipment still block when a service
requirement is disabled. Reported action risk and sensor/operator observations are
inputs, not independent verification of physical conditions.

An operator posts to `POST /actions/{action_id}/interventions` with `operator_id`,
the current `decision_id`, `kind` (`override` or `reject`), a nonempty `reason`, and
at least one evidence item. The response records whether the intervention was
accepted and why. Refused interventions are also retained.

An override requires every outstanding check to be an explicitly overridable
escalation. It cannot waive a hard block or missing/expired required evidence.
The original policy decision is preserved; `effective_decision` and `overridden_by`
record the authorized exception separately. Approval applies once to that action's
exact policy revision, source records, and gate results. Changed evidence or an
expired gate invalidates approval at execution. Stale reviews require a fresh
decision ID. `reject` prevents subsequent execution without deleting prior events.

## Historical API

All routes below use the `/v1/instances/{instance_id}` prefix:

| GET route | Contents |
|---|---|
| `/actions` | Action summaries; optional `task_id` |
| `/actions/{action_id}` | Proposal, current state, and full ordered provenance |
| `/action-events` | Proposed, started, rejected, completed/failed, and intervention events |
| `/policy-decisions` | Decision-bearing events with policy/SOP revisions, evidence, and gate results |
| `/incidents` | Incidents linked by instance, action, task, and originating event |
| `/interventions` | Accepted/refused operator decisions and evidence linked to action/task/event |
| `/policies` | Immutable policy revision history |

History collections accept optional `action_id`/`task_id` filters and
`limit` (1–200, default 100) / `offset`. Action summaries and policies also paginate.
Events have per-action sequence numbers; IDs break ties in collection ordering.
Incidents remain historical entries even after an override; their associated
interventions and current action state show what happened next. Incident case
closure and external notifications are not implemented in this phase.

New action/risk-decision/incident/intervention writes through generic `/records`
return 409, preventing that endpoint from fabricating unaudited lifecycle entries.
Previously stored #22 records remain readable as legacy records, but do not
authorize execution. The sampling POC's separate SQLite controller is unaffected.

Audit events have no update/delete API. They survive restarts and capture their
original evidence even after new observations or policy revisions are installed.
This is application-level append-only history, not tamper-proof storage against a
database administrator. Instance writes use short database locks; those locks
cover evidence selection and dispatch intent, not continuous physical interlocks
or device operation after the adapter starts.

## Verification

```bash
python -m pip install -e ".[test]"
python -m unittest discover -s tests -v
```

CI runs the full suite on SQLite and the action suite on a real PostgreSQL service.
To run the action suite on your own **disposable test database**, set
`CLEANROOM_TEST_DATABASE_URL` to its PostgreSQL URL, then run
`python -m unittest discover -s tests -p test_action_runtime.py -v`.
Tests use distinct instance IDs and retain their audit data in that test database.
Coverage includes gates, exact-decision overrides, unauthorized writes, atomic
pre-dispatch persistence, concurrent workers, adapter failure, restart durability,
instance scoping, and historical inspection.
