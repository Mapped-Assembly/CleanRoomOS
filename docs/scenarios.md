# Current synthetic acceptance scenarios

The original conversation scenarios are [archived](legacy/conversation-scenarios.md).
Use the [Python controller demo](controller.md) and [agent setup](agents.md).

| Case | Expected result |
|---|---|
| Offline baseline | A/C fit; B keeps both required samples and a conflict |
| Explicit manufacturing schedule replacement | New revision; only addressed conflicts resolve |
| Normal LIMS fixture with fixture plan | Six matches; review_ready until a human QA decision |
| Deliberate LIMS anomalies | Five sourced findings; approval cannot waive them |
| Requirements Agent fabricates count/threshold/source | Rejected; context_pending and error event |
| Planning Agent fabricates availability | Independent validator rejects; no accepted plan |
| Review Agent omits a finding or invents a source | Rejected; results_received with error event |
| Agent emits an approval field | Schema rejects; no human decision is recorded |
| Direct subagent invocation | Reply only; SQLite state and event ledger unchanged |
| Missing model, provider refusal, malformed reply, timeout | Visible bounded error; no automatic fallback |
| Identical LIMS re-import | Snapshot replaced; no duplicate findings; old review invalidated |

The default test suite exercises these boundaries offline. The opt-in real-model test
in docs/agents.md exercises all three roles through actual OpenCode calls when an
accessible model is configured. Live acceptance status is documented there.
