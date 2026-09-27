# QA review packages and local inbox

The Python controller builds a versioned package from the exact run evidence and
creates one local QA notification in the same SQLite transaction. No email, Slack,
external service or equipment operation is involved.

## Follow the evidence

Every controller-prepared package contains:

- Full validated `context`: versioned SOP/recipe requirements, rooms, schedule
  access/occupancy/shift constraints and explicitly supplied execution inputs.
- Exact plan ID/revision, original obligations, sample sequence/timing, citations
  and unresolved conflicts.
- Complete LIMS result snapshot, expected/received/matched/blocked/missing counts,
  deterministic findings and their sample/requirement/result/source links.
- `prior_decisions`: accepted planning and QA decisions from this database's event
  ledger, retaining actor, role, exact revisions, timestamp, rationale and sources.
  Failed or stale decision attempts remain error events, not accepted history.
- `collection_status`, `completeness`, a concise current-run `summary`, and explicit
  `required_actions`. Summaries do not invent trends or cross-run analytics.

Controller enrichment computes canonical findings/counts even if a draft service
omits them. A clean result set alone cannot imply collection or QA approval. The
package contains decisions that preceded its creation; a subsequent QA decision is
recorded separately in the event ledger, without mutating the archived package.

## Local review controls

After preparing a plan or importing results:

```bash
python -m cleanroom_os.workflow review --db cleanroom-demo.sqlite
python -m cleanroom_os.workflow notifications --db cleanroom-demo.sqlite
```

Each notification includes the exact package and plan revisions, QA audience,
completeness, status, required action and a logical target such as
`cleanroom://qa/packages/QA-PACKAGE/revisions/1`. This is a local queue identifier;
there is no hosted page or OS URI handler. Resolve it through the local package reader:

```bash
python -m cleanroom_os.workflow package --db cleanroom-demo.sqlite --package-id QA-PACKAGE --package-revision 1
```

Use the IDs/revisions from the actual notification. `package` and `notifications`
are read-only. Historical superseded packages remain retrievable, but cannot receive
a new decision as though they were current. Notify retry is explicit and idempotent:

```bash
python -m cleanroom_os.workflow notify-qa --db cleanroom-demo.sqlite
```

The unique package ID/revision key prevents duplicate queue entries, including after
restart. Retries do not reset rejected/approved status or timestamps. Package archival,
notification, snapshot and event writes commit together. A queue failure rolls back
the proposed package and revision; the failed attempt is recorded visibly.

The final QA controls are:

```bash
python -m cleanroom_os.workflow qa-approve --db cleanroom-demo.sqlite --role qa --revision 2 --package-revision 1 --reason "Reviewed complete synthetic evidence"
python -m cleanroom_os.workflow qa-reject --db cleanroom-demo.sqlite --role qa --revision 2 --package-revision 1 --reason "State the evidence requiring correction"
python -m cleanroom_os.workflow qa-resolve --db cleanroom-demo.sqlite --role qa --revision 2 --package-revision 1 --reason "State the unresolved inputs or findings"
```

These are alternatives, not a sequence to execute. Each requires the currently
reviewed plan/package revisions. Manufacturing cannot submit any final QA decision.
Roles remain self-reported in this local POC; this is not authenticated identity.

## Incomplete packages and recovery

A blocked plan or a plan without recorded collection can enter `review_ready` with
an **incomplete** package. That state means review is available, not that sampling
is complete. Findings, required counts and blockers remain visible; QA may reject
or request resolution, but approval fails. Preparing a review never simulates
collection or authorizes a plan.

The controller retains the pre-review operational stage. An incomplete preview does
not prevent manufacturing from allowing an otherwise complete proposed plan or from
recording collection for an already approved plan. Those actions invalidate the old
preview, marking its notification superseded. They cannot authorize a blocked plan.

| Notification status | Meaning / next action |
|---|---|
| `awaiting_qa` | Review this exact package; incomplete evidence cannot be approved |
| `approved` | Separate QA decision recorded; subsequent evidence changes require review |
| `rejected` | Read QA rationale; correct inputs/results and prepare a new package |
| `resolution_requested` | Manufacturing must address cited source or result issues |
| `superseded` | Historical evidence; use the current notification for decisions |

The snapshot's `action_required` includes the rejection/resolution rationale. Correct
planning constraints through `context`, which clears collection, results and approval
and requires a new plan revision. For an already collected plan, corrected LIMS inputs
can be imported after rejection/resolution; that clears the old package and requires
a new package revision. Result imports are still forbidden before recorded collection.
Re-reviewing unchanged evidence immediately after a resolution request is rejected.

Any source/result change (including an identical re-import) invalidates the current
package and supersedes its notification; stale QA decisions are rejected. Original
packages and accepted decisions remain inspectable. Merely reading the inbox or
retrying notification never changes review evidence.

## Compatibility and tests

Older controller snapshots infer recorded collection only from states that the prior
controller could reach after its enforced collection gate. Legacy packages lacking
these new context/completeness fields cannot be newly approved; re-import results
and prepare a new package. Existing accepted decisions stay in the ledger.

```bash
python -m unittest discover -s tests -v
```

Tests cover source tracing, incomplete review, queue atomicity and retry, exact
archived lookup, manufacturing/QA separation, stale decisions, actionable rejection,
resolution recovery and distinct collection/result/decision records.
