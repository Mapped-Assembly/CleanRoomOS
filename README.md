# CleanRoomOS

An OpenCode simulation of continuous interaction between cleanroom operators, manufacturing staff, QA, and three specialist AI subagents.

| Subagent | Responsibility |
| --- | --- |
| `cleanroom-operation` | Generate one reviewable task from shift context |
| `cleanroom-risk` | Classify risk as `none`, `low`, `medium`, or `high`; attempt de-risking for medium/high tasks |
| `cleanroom-compliance` | Produce the **Tracking and trending report** from task, risk, decision, and outcome records |

The `cleanroom` primary agent coordinates these three subagents. Medium/high **residual** risk pauses the cycle for manufacturing or QA:

> Operation Agent is attempting to do {Task} allow or disallow.

Both role audiences are named in the conversation. Either selected role can decide in this initial demo. Approval applies only to the displayed task revision. Disallowed work does not proceed. Low/none residual risk can proceed to a clearly labeled hypothetical outcome. Reports retain initial risk, attempted mitigations, residual risk, human decisions, and pending work.

## Run

Install and configure [OpenCode](https://opencode.ai/docs/) with your chosen model/provider, then:

```bash
git clone https://github.com/isayahc/CleanRoomOS.git
cd CleanRoomOS
opencode --agent cleanroom
```

Enter `/cleanroom` and provide your scenario, observations, and role (`manufacturing` or `qa`). You can also pass context directly, for example `/cleanroom I am QA. Simulate a shift reviewing incomplete monitoring records.`

When prompted, respond with `allow T001 r1` or `disallow T001 r1`, substituting the displayed ID and revision. Use `continue`, new observations, `report`, or `stop` to steer subsequent cycles. This is turn-by-turn interaction, not an unattended background process.

See [synthetic scenarios and manual acceptance checks](docs/scenarios.md). Agent definitions use OpenCode's [documented configuration format](https://opencode.ai/docs/agents/). No provider/model is hardcoded, and no Python dependency is required.

## Scope

This initial version consists of OpenCode agent configuration and prompts. It does not control equipment, send external notifications, authenticate reviewer roles, persist an audit database, or certify compliance. The ledger lives in the conversation; retain the report and event ledger before ending a session. Prompt instructions guide the simulation, but a production approval gate requires a separately enforced state machine and authenticated decision storage.

All four agents deny operational tools. Only the coordinator can invoke the three named subagents; specialists return their assessments as text. File read/search tools are available for supplied context. A configured OpenCode installation and model access are required to exercise live delegation.

## Shared SOP context

[CR-SOP-001: Cleanroom Operations, Task Review, and Exception Handling](docs/cleanroom-sop.md) is the shared simulation SOP. `opencode.json` loads the complete file through project-level `instructions`, so the coordinator and all three subagents receive it, including direct subagent invocations. Each role prompt also requires reading the SOP if it is missing from context and pausing if it cannot be obtained. Keep the SOP in this one file when revising it.

After pulling this update, restart OpenCode to reload the project configuration. Facility-specific placeholders remain unapproved until manufacturing and QA complete them.

## Python contracts

The sampling POC now has typed JSON exchange contracts in `cleanroom_os.contracts`.
See [contract usage, authority rules, and validation boundaries](docs/contracts.md).

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

This contract layer is independent of the existing OpenCode conversation simulation.

## Mock facility and offline inputs

[The synthetic three-room scenario](fixtures/mock-facility/README.md) includes a blocked Room B, an explicit human-supplied replacement schedule, normal/anomalous LIMS results, and resolvable source evidence. File-backed adapters implement `cleanroom_os.adapters.InputAdapter`.

```bash
python -m cleanroom_os.fixtures fixtures/mock-facility
python -m cleanroom_os.fixtures fixtures/mock-facility --resolved --normal-results
```

These commands validate and summarize inputs; they do not approve or execute a plan.

## Deterministic workflow controller

The Python controller now persists revision-specific decisions and workflow state in SQLite, with independent validation gates and an offline, stepwise CLI. See [controller states, demo commands, and boundaries](docs/controller.md). The existing OpenCode conversation does not control this persistent workflow.

## Deterministic sampling planner

The workflow CLI now generates plans from validated SOP, recipe, schedule and explicit
execution constraints. Blocked obligations remain visible; an independent validator
checks every proposal before the controller accepts it. See [planning policy, conflicts,
and revision handling](docs/planning.md). Use `propose --fixture-plan` only to replay
the original hand-authored plan/LIMS demonstration.

## LIMS matching and review findings

The workflow `review` action now evaluates results against the exact plan revision,
retaining LIMS evidence and identifying missing, unmatched, duplicate, mismatched,
out-of-limit and unknown results. [Reconciled counts](docs/evaluation.md) distinguish
blocked obligations from expected samples. Evaluation prepares the review; QA still
makes the final decision explicitly.
