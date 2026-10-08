# CleanRoomOS

CleanRoomOS is a [Mapped Assembly](https://github.com/Mapped-Assembly) proof of
concept for local cleanroom sampling workflows: preserve SOP/recipe obligations,
plan around room constraints, match simulated LIMS results, and prepare evidence
for a separate human QA decision.

## Interactive demo

Install Git, Python 3.11+, and OpenCode connected to a model you can access.
On Windows, the Python launcher (`py -3`) must be available; Linux/macOS use
`python3`. From a terminal:

```powershell
git clone https://github.com/Mapped-Assembly/CleanRoomOS.git
cd CleanRoomOS
opencode --agent cleanroom
```

Then type **“Plan today’s sampling.”** The assistant creates a local `.venv` and
installs the Python dependencies on first use (package-download access is needed),
then walks you through room conflicts, manufacturing approval, simulated
collection, lab results, and human QA review. No separate server, password, or model
environment variable is needed. Your existing OpenCode model selection is used.

The interactive demo uses the bundled synthetic fixture plan/results through the
validated Python controller. State persists in `cleanroom-interactive.sqlite`.
Manufacturing and QA decisions require your explicit confirmation.

The Python controller owns persisted state and validation. Three optional OpenCode
agents return bounded, cited proposals; they cannot execute tasks or approve work.

| Role | Responsibility | Enforced boundary |
|---|---|---|
| Requirements Agent | Extract applicable structured SOP/recipe requirements and gaps | Exact authoritative counts, thresholds and citations must survive validation |
| Planning Agent | Propose sequence and timing | Independent checks of obligations, access, occupancy, duration and resources |
| Results-review Agent | Explain findings and draft a QA summary | Controller-computed evidence, findings and counts cannot be changed |
| Manufacturing | Supply constraints and explicitly allow/disallow a plan revision | Conflicts cannot be waived |
| Human QA | Approve, reject or request resolution for an exact package | Separate decision with independent completion checks |

## Developer CLI: offline

No model or credentials are required. From the cloned repository, install the
Python package in a virtual environment. Linux/macOS or WSL2:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Windows PowerShell, without activating the environment:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

After installation, these commands run offline. In PowerShell, replace `python`
with `.\.venv\Scripts\python.exe` if the environment is not activated. Run from
the repository root so the bundled fixtures are available:

```bash
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

## Developer live-adapter acceptance test

With Python 3.11+, Git, and an authenticated OpenCode installation, run the live
acceptance demo from PowerShell in this checkout:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-live-demo.ps1
```

This uses your existing OpenCode model selection. No password is required unless
`OPENCODE_SERVER_PASSWORD` is already set. The demo starts and stops its own localhost
server and exercises all three agents with synthetic data through `review_ready`.


Configure an OpenCode provider/model you can access, start `opencode serve` from this
repository, and use `--mode opencode` on `context`, `propose`, and `review`.
`OPENCODE_MODEL` must name an explicit `provider/model`; there is no model or offline
fallback. See [Windows setup, role contracts, live tests and migration](docs/agents.md).

`opencode`, `opencode --agent cleanroom`, and `/cleanroom` open the interactive
controller-backed assistant. The old autonomous
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
python -m pip install -e ".[test]"
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

## Repository location

The canonical repository is
[`Mapped-Assembly/CleanRoomOS`](https://github.com/Mapped-Assembly/CleanRoomOS).
For an existing checkout, update its remote from inside that directory:

```bash
git remote set-url origin https://github.com/Mapped-Assembly/CleanRoomOS.git
```

The Python distribution `cleanroom-os`, import package `cleanroom_os`, OpenCode
agent `cleanroom`, and local SQLite filenames are unchanged by the transfer.
