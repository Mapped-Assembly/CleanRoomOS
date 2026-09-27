# Three bounded OpenCode adapters

The entry point is `python -m cleanroom_os.workflow`, not a conversational coordinator.
`--mode offline` (default) uses deterministic requirements/planning/review adapters.
`--mode opencode` calls the three configured OpenCode roles through data-only HTTP
sessions. The mode is explicit per action, and a live failure never switches modes.

## Windows / PowerShell setup

Install Python 3.11+, OpenCode, and configure a provider through `opencode auth login`.
Use a model listed by `opencode models` that your provider account actually permits.
No model is hardcoded in this repository. From the repository root:

```powershell
python -m pip install -e .
$env:OPENCODE_SERVER_PASSWORD = "your-local-server-password"
opencode serve --hostname 127.0.0.1 --port 4096
```

In another PowerShell terminal in the same repository, set the same server password
and your selected model:

```powershell
$env:OPENCODE_SERVER_PASSWORD = "your-local-server-password"
$env:OPENCODE_BASE_URL = "http://127.0.0.1:4096"
$env:OPENCODE_MODEL = "provider/model-from-your-configured-account"
python -m cleanroom_os.workflow context --db agent-demo.sqlite --mode opencode
python -m cleanroom_os.workflow propose --db agent-demo.sqlite --mode opencode
python -m cleanroom_os.workflow status --db agent-demo.sqlite
```

Do not use the placeholder model literally. The base URL defaults to localhost:4096;
`OPENCODE_SERVER_USERNAME` defaults to `opencode`. A remote endpoint must use HTTPS.
`--model provider/model` overrides `OPENCODE_MODEL`. `--agent-timeout` is 1–300 seconds
per attempt (default 120). Source payloads and schemas are sent to the selected model.

B remains blocked until manufacturing explicitly supplies the valid replacement:

```powershell
python -m cleanroom_os.workflow context --db agent-demo.sqlite --mode opencode --resolved --reason "Manufacturing supplied the replacement schedule"
python -m cleanroom_os.workflow propose --db agent-demo.sqlite --mode opencode
```

Inspect the resulting plan revision before the human `allow` action. Collection,
result ingestion and final QA decisions use the same controller actions as offline
mode. For `review`, select `--mode opencode`. Original hand-authored LIMS fixtures
reference different sample IDs from generated plans; do not relabel those records
or expect them to match. The live acceptance test below supplies fresh synthetic
observations for its actual generated plan.

## Contracts and gates

Every role returns `AgentResponse` with `output`, `assumptions`, and `gaps`. Prose,
fenced JSON, extra fields and incomplete responses fail. Assumptions are reported as
`Unknown` and prevent acceptance; they must be resolved by human source input. Explicit
source gaps retain their exact reasons/citations and cannot disappear from a reply.

| Role | Supplied data | Allowed output | Deterministic gate |
|---|---|---|---|
| `cleanroom-requirements` | Applicable structured SOP/recipe source documents | Exact extraction and explicit gaps | Controller compares documents, counts, thresholds and citations to originals |
| `cleanroom-planning` | Validated context plus deterministic baseline | Samples and conflicts only | Immutable requirements are reattached; independent schedule/evidence/ID validation |
| `cleanroom-results-review` | Exact plan/results and deterministic evaluation | Cited summary and one explanation per finding | Canonical plan, results, findings and counts are assembled in Python and rechecked by controller |

The planning schema has no requirements, access, or approval fields. The review
schema has no thresholds, findings replacement or decision fields. Model narrative
is stored as labeled `agent_explanation` JSON within the package; it is untrusted
commentary, not evidence of execution or approval. The deterministic evidence remains
the source of truth for reviewers and gates.

All roles deny tools, including shell, file access, network tools, Task and MCP calls.
The HTTP client verifies the server's effective project configuration before creating
a fresh session and applies a session-wide deny rule. It pins the agent and model,
uses `prompt_async`, polls to a fixed deadline, rejects tool-call parts and model/agent
substitution, and aborts its own session when finished or failed. It never grants
permissions or invokes decisions. Response size is bounded. Local sessions remain
available for troubleshooting; provider error bodies and credentials are not copied
into the controller's event ledger.

Requirements and planning have at most two controller attempts; review has one.
Failures are visible exceptions and durable error events, not inferred passes. A
requirements failure invalidates context and dependent approvals. Other failed
proposals preserve the last committed snapshot. Calls serialize writes within a
single local run; run one mutating CLI action at a time. Roles are not OS isolation:
a person with Python/database access is trusted, and a configured OpenCode server
is trusted infrastructure. Do not add controller/decision tools to the agents.

## Tests and live acceptance

```powershell
python -m unittest discover -s tests -v
# Requires the server above and a genuinely accessible configured provider/model:
$env:CLEANROOM_LIVE_OPENCODE = "1"
python -m unittest discover -s tests -p test_live_agents.py -v
Remove-Item Env:CLEANROOM_LIVE_OPENCODE
```

The live test creates a temporary synthetic run, invokes all three roles through the
controller, supplies an explicit test-only manufacturing decision, simulates
collection, and evaluates newly created synthetic LIMS observations. It stops at
`review_ready`, never QA approval. No model credentials means the default suite skips
this one test. Explicit live mode never skips provider failures or substitutes mocks.

Implementation verification used OpenCode 1.18.32. The real HTTP configuration/session
path was exercised, but the `opencode/big-pickle` provider rejected the model request
with HTTP 403 stating its free tier is restricted to use within OpenCode. Therefore
successful three-role live-model acceptance remains unverified until an accessible
provider/model is configured. Do not treat the passing offline tests as that acceptance.

## Migration and remaining dependency

The previous operation/risk/compliance prompts and revision 0.1 SOP/scenarios are
archived under `docs/legacy`. They are not referenced by active configuration.
`/cleanroom` now routes only to entry-point help; it cannot delegate or approve work.
The active shared SOP is revision 0.2. Restart OpenCode after updating the config.
Reload context through the controller to create a fresh validated run revision.

Issue #6 remains open for the local notification queue and expanded QA package
workflow. These adapters use the existing package and final-decision gates, and do
not claim to implement that separate dependency.

API references: [OpenCode server](https://opencode.ai/docs/server/),
[agents](https://opencode.ai/docs/agents/), and
[permissions](https://opencode.ai/docs/permissions/).
