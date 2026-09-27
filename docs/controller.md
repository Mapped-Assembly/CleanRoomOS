# Deterministic local workflow controller

`cleanroom_os.controller.Controller` owns a single synthetic run per SQLite database. Calls read the latest snapshot inside `BEGIN IMMEDIATE`; the snapshot and event are committed together. Failed actions are recorded with their type/message and original state, then raised as `WorkflowError`. Loading invalid replacement context instead invalidates downstream approval and returns to `context_pending`. Callers must surface these exceptions.

| Action | Required state | Result |
|---|---|---|
| Human context load/update | Any | validated; plan/results/package invalidated |
| Planner proposal | validated or unresolved proposal, no pending human resolution | plan_proposed or blocked |
| Human allow | plan_proposed (including an incomplete review of that stage), exact revision | plan_approved |
| Human disallow/request resolution | proposed or blocked plan, exact revision | blocked |
| Simulated collection | plan_approved (including an incomplete review of that stage), manufacturing role | collection_simulated |
| LIMS import | Collection recorded; includes subsequent QA rejection/resolution | results_received; package/QA approval invalidated |
| Review proposal | Current plan/context; blocked or uncollected plans may be reviewed | review_ready |
| QA decision | review_ready, exact package and plan revisions | qa_approved, qa_rejected, or blocked |

Blocked obligations remain on the plan. Allow cannot waive a conflict or unknown requirement. Human context updates must include attribution and reason; subsequent proposals use strictly increasing plan revisions. Historical source snapshots, decisions, and failed operations remain in `events`. Package revisions also increase, and decision IDs cannot repeat. Restarting `Controller` with the same path resumes the run. It does not replay or manufacture human decisions.

`ProposalService` and `ReviewService` receive serialized input and return JSON; they do not receive controller handles. The planner has at most two attempts; review has one. `DeterministicPlanner` generates proposals from current inputs by default. `FixturePlanner` remains an explicit oracle replay option for versions 1/2. See [the planning policy and independent validator](planning.md). `DeterministicReviewer` matches LIMS records and produces sourced findings and reconciled counts. See [evaluation semantics](evaluation.md). `FixtureReviewer` is now a compatibility wrapper for deterministic review. The controller rejects packages that alter or omit deterministic findings, counts or source evidence; its separate final gate also checks raw results. The controller attaches full source context, collection status, accepted human history, summary and required actions, then atomically archives the package and enqueues a [local QA notification](qa-review.md). The conservative POC gate does not allow findings to be waived.

## Offline interaction

Run from the repository root after `python -m pip install -e .`. All roles below are **self-reported and unauthenticated**. These commands simulate a sequence; each decision command is a separate explicit human action.

```bash
python -m cleanroom_os.workflow context --db /tmp/cleanroom-demo.sqlite
python -m cleanroom_os.workflow propose --fixture-plan --db /tmp/cleanroom-demo.sqlite
# B is blocked; allow at revision 1 is rejected.
python -m cleanroom_os.workflow context --db /tmp/cleanroom-demo.sqlite --resolved --reason 'Manufacturing supplied the fixture schedule update'
python -m cleanroom_os.workflow propose --fixture-plan --db /tmp/cleanroom-demo.sqlite
python -m cleanroom_os.workflow allow --db /tmp/cleanroom-demo.sqlite --revision 2
python -m cleanroom_os.workflow collect --db /tmp/cleanroom-demo.sqlite
python -m cleanroom_os.workflow results --db /tmp/cleanroom-demo.sqlite --normal
python -m cleanroom_os.workflow review --db /tmp/cleanroom-demo.sqlite
python -m cleanroom_os.workflow qa-approve --db /tmp/cleanroom-demo.sqlite --role qa --revision 2 --package-revision 1 --reason 'Reviewed the synthetic complete result set'
python -m cleanroom_os.workflow events --db /tmp/cleanroom-demo.sqlite
```

Use a fresh database path for each demo run; existing runs are never reset implicitly. Omit `--normal` to load the anomalous data, which the final approval gate rejects. `status` is read-only. Context adapter/file errors and controller validation/service errors enter the ledger. CLI LIMS file errors are displayed before ingestion.

## Boundaries

This implements a local POC control boundary, not authentication or tamper-proof audit storage. A caller with Python/SQLite access is trusted; do not expose decision methods as tools to untrusted model code. Human identity/role is checked structurally only. Sources and exact evidence are retained, but authenticity of upstream documents is outside this local adapter. The shared independent validator enforces availability, occupancy, sequence, duration, setup, travel and technician/shift bounds. Only local SQLite QA notifications are created; no external messages, live systems or equipment are invoked. The old OpenCode conversation-only demo is retired. The three [bounded OpenCode adapters](agents.md) are selected with `--mode opencode`; only this controller advances the database.
