# Synthetic facility: one reproducible sampling scenario

All files in this directory are **synthetic demonstration data**, not facility instructions or regulatory limits. No credentials, network calls, DMS, LIMS service, or physical equipment are used.

## Inputs and authority

| File | Purpose |
|---|---|
| `sop.json` | Authoritative sampling requirements for this fictional facility only: two air samples per room, synthetic upper bound ≤10 CFU/m3 |
| `recipe.json` | Same six required samples with SOP and recipe references |
| `facility.json` | Three room identities and explicit execution assumptions |
| `schedule.json` | Baseline daily occupancy/access, version 1 |
| `human-update.json` | Explicit fictional manufacturing input freeing Room B; not an approval |
| `schedule-resolved.json` | Human-supplied replacement schedule, version 2 |
| `lims-normal.json` | Six normal results for resolved plan revision 2 |
| `lims-anomalies.json` | Seven records containing normal and deliberately problematic data for revision 2 |
| `sources.json` | Maps source kind/ID/version to files; locators are JSON Pointers |
| `plan-blocked.json`, `plan-resolved.json` | Hand-authored expected outputs for future planner tests, not generated/approved plans |
| `expected-findings.json` | Test oracle for future results-review implementation |

The existing `docs/cleanroom-sop.md` (CR-SOP-001) remains a workflow simulation draft. Its placeholders are not sampling requirements. `sop.json` is the separate sampling SOP fixture; no real sampling procedure is implied.

## Time and resource assumptions

Date: 2026-09-27. Time zone: America/New_York (UTC−04:00 on this date). Shift: 09:00–17:00. One sampling technician, TECH-001, is available for the shift; visits and samples are sequential. Each sample takes five minutes. Room setup takes five minutes and occurs inside the access window. Travel between any two rooms takes five minutes; the technician starts at Room A. Waiting is allowed. No other resource or timing constraints are implied.

| Room | Baseline access | Occupancy | Required samples |
|---|---|---|---|
| A | 09:00–09:30 | 09:30–17:00 | 2 air |
| B | None | 09:00–17:00 | 2 air, retained while blocked |
| C | 10:00–17:00 | 09:00–10:00 | 2 air |

The baseline expected proposal collects A at 09:05–09:15 and C at 10:05–10:15, and retains B as an unresolved conflict. This partial proposal does not complete or approve the plan.

The explicit fictional human update creates B access at 11:00–11:30 and replaces its occupancy with 09:00–11:00 and 11:30–17:00. The resolved expected plan adds B at 11:05–11:15. SOP, recipe, sample counts, and sample types stay unchanged. Source update and plan approval are distinct steps; the future controller must require the appropriate human action before proceeding.

## LIMS anomalies

| Case | Record/sample |
|---|---|
| Normal | SAMPLE-A-2 and SAMPLE-B-1 |
| Missing | No record for SAMPLE-B-2 |
| Duplicate | RES-1 and RES-DUPLICATE both concern SAMPLE-A-1; record IDs remain distinct |
| Unmatched | RES-UNMATCHED concerns SAMPLE-UNPLANNED |
| Mismatch | SAMPLE-C-2 reports room A instead of C |
| Out of limit | SAMPLE-C-1 reports 25 CFU/m3 against the synthetic ≤10 CFU/m3 requirement |

Both LIMS variants intentionally refer to plan revision 2. They represent the **later** result-import phase after explicit schedule resolution and plan approval. Loading them alongside baseline inputs for inspection does not authorize a blocked plan or imply sample collection happened.

## Offline usage

From the repository root:

```bash
python -m pip install -e .
python -m cleanroom_os.fixtures fixtures/mock-facility
python -m cleanroom_os.fixtures fixtures/mock-facility --resolved --normal-results
python -m unittest discover -s tests -v
```

Programmatic consumers depend on `InputAdapter`; the file-backed implementation is `FileInputAdapter(Path(...))`. `load_context(adapter)` validates shared inputs. Replacement implementations can retrieve the same contracts from other storage later. Errors remain visible; missing files never trigger network fallback or invented defaults. `resolve_source(reference)` resolves local evidence for downstream views.

This issue supplies data/adapters and static expected-output oracles. Scheduling, persisted workflow transitions, automatic LIMS evaluation, and actual QA notifications belong to subsequent issues.
