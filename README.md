# CleanRoomOS

A local cleanroom sampling POC: preserve SOP/recipe obligations, plan sampling around
actual room constraints, match simulated LIMS results, and prepare evidence for a
separate human QA decision.

The Python controller owns persisted state and validation. Three optional OpenCode
agents return bounded, cited proposals; they cannot execute tasks or approve work.

| Role | Responsibility | Enforced boundary |
|---|---|---|
| Requirements Agent | Extract applicable structured SOP/recipe requirements and gaps | Exact authoritative counts, thresholds and citations must survive validation |
| Planning Agent | Propose sequence and timing | Independent checks of obligations, access, occupancy, duration and resources |
| Results-review Agent | Explain findings and draft a QA summary | Controller-computed evidence, findings and counts cannot be changed |
| Manufacturing | Supply constraints and explicitly allow/disallow a plan revision | Conflicts cannot be waived |
| Human QA | Approve, reject or request resolution for an exact package | Separate decision with independent completion checks |

## Start offline

No model or credentials are required. Python 3.11+ is required.

```bash
python -m pip install -e .
python -m cleanroom_os.workflow context --db cleanroom-demo.sqlite
python -m cleanroom_os.workflow propose --db cleanroom-demo.sqlite
python -m cleanroom_os.workflow status --db cleanroom-demo.sqlite
```

The synthetic baseline schedules A within 09:00–09:30, C after 10:00, and retains B's
blocked obligations. Manufacturing must explicitly supply the replacement schedule
before a new revision can resolve B. Approval, simulated collection, result entry,
and QA decisions are separate actions.

Follow the [full offline controller demo](docs/controller.md) for normal/anomalous
LIMS replay and human decisions. Use `--fixture-plan` for the hand-authored LIMS
fixtures; generated plans have their own stable sample IDs.

## Use OpenCode agents

Configure an OpenCode provider/model you can access, start `opencode serve` from this
repository, and use `--mode opencode` on `context`, `propose`, and `review`.
`OPENCODE_MODEL` must name an explicit `provider/model`; there is no model or offline
fallback. See [Windows setup, role contracts, live tests and migration](docs/agents.md).

`opencode` or `/cleanroom` displays entry-point help only. The old autonomous
operation/risk/compliance conversation is [retired](docs/legacy/README.md). Direct
subagent calls cannot change the SQLite workflow.

## Evidence and checks

- [Shared workflow SOP](docs/cleanroom-sop.md): authority and human decisions
- [Typed contracts](docs/contracts.md): JSON boundaries and source identity
- [Synthetic facility](fixtures/mock-facility/README.md): rooms, constraints and source evidence
- [Planning](docs/planning.md): scheduling, structured conflicts and revisions
- [LIMS evaluation](docs/evaluation.md): exact-revision matching and reconciled counts
- [Scenarios](docs/scenarios.md): current offline and agent acceptance cases

```bash
python -m unittest discover -s tests -v
```

Default tests run offline, including fake-HTTP OpenCode protocol tests and injected
invalid responses. The real-model integration test is opt-in and fails visibly if
the configured provider rejects the request.

## Scope

This POC does not control equipment, connect to live DMS/LIMS, authenticate human
roles, or certify compliance. SQLite provides local persistence, not tamper-proof
audit storage. Requirements extraction currently consumes supplied structured source
fixtures; arbitrary PDF/text extraction is not implemented.

## QA package and local inbox

Full source context, plan sequence, deterministic findings and accepted human decision
history are retained in immutable packages. Blocked/uncollected plans remain explicitly
incomplete. A transactional [local QA inbox](docs/qa-review.md) tracks exact revisions,
QA decisions and superseded evidence without external messaging.
