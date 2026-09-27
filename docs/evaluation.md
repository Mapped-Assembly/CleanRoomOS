# Deterministic LIMS evaluation

`cleanroom_os.evaluation.evaluate_results(plan, results)` matches a complete LIMS
snapshot to an exact plan ID and revision. It returns `ResultEvaluation`: plan
identity, reconciled counts, evidence references and findings. It never approves a
plan or makes a final compliance decision.

`DeterministicReviewer(revision)` implements the controller's review-service
interface and includes the evaluation in `QAReviewPackage`, retaining the exact
plan and all original results. The workflow CLI uses it for `review` by default.
The controller only permits result ingestion after approval and simulated collection;
it then supplies that same plan to the reviewer. Direct evaluator calls can also
explain a blocked proposal, but cannot authorize its execution.

## Matching and findings

Records first match by planned-sample ID. Plan ID/revision, room, sample type and
collection time must then match the supplied plan. Collection timestamps must lie
within the sample window, including either endpoint, consistent with the existing
controller gate. A stale revision or wrong context produces a `mismatch` finding;
records are never reassigned to another sample or plan. Thresholds are not applied
to records with the wrong collection context.

| Finding kind | Meaning |
|---|---|
| `missing` | A scheduled sample has no received record with its sample ID |
| `unmatched` | A received record refers to an ID absent from this plan |
| `duplicate` | Multiple distinct result records refer to the same sample ID; every record is retained |
| `mismatch` | Wrong plan/revision, room, sample type, collection time, or incompatible measurement unit |
| `out_of_limit` | A context-matched measurement fails the supplied authoritative comparison |
| `unknown` | Authoritative threshold or matched measurement is explicitly unknown |
| `blocked` | A requirement has unscheduled samples, unknown count, or unresolved planning conflicts |

Each finding has a machine-readable `code`, stable finding ID, sample/requirement
links where applicable, result IDs, a description, and source references. IDs derive
from plan identity/revision and the finding's actual evidence; repeated evaluation
and reordered records produce the same findings. Missing/blocked findings reference
plan and requirement evidence; threshold findings include SOP/recipe threshold
sources. Unknown or extra records retain their original LIMS source identity/version.

Only supplied `lt`, `le`, `gt`, `ge` or `eq` comparisons are used. Units must match
exactly; no conversion or default threshold is invented. Duplicate measurements are
all evaluated, so a normal duplicate cannot hide an out-of-limit result.

## Counts

| Count | Definition |
|---|---|
| `known_required` | Sum of known requirement counts |
| `expected` | Scheduled sample count |
| `blocked` | Known required samples not scheduled |
| `unknown_count_requirements` | Obligations whose counts cannot be quantified |
| `received` | Number of distinct LIMS result records in this import |
| `matched` | Expected sample IDs with at least one record matching all collection context fields |
| `missing` | Expected sample IDs with no received record |
| `mismatched_samples` | Expected sample IDs with records, but none with valid collection context |
| `matched_records` | Records matching an expected sample and all collection context fields |
| `mismatched_records` | Records for expected sample IDs with invalid collection context |
| `unmatched_records` | Records for IDs absent from the plan |
| `duplicate_records` | Records beyond the first within each sample-ID group; overlaps the above categories |

A match is an identity/context match, **not a measurement pass**. Unknown values,
incompatible units, threshold violations and duplicates still produce findings.
A stale-only sample is counted as mismatched rather than also counted as missing.

The contract enforces these identities:

- `known_required = expected + blocked`
- `expected = matched + missing + mismatched_samples`
- `received = matched_records + mismatched_records + unmatched_records`

Unknown counts are reported separately, never converted to zero required samples.
For the deliberate anomaly fixture: 6 expected samples, 7 received records,
4 matched samples, 1 mismatched sample, 1 missing sample, and 1 excess duplicate
record. Blocked Room B in the baseline remains a blocked obligation; it is not
counted as a scheduled sample with a missing result.

## Imports and demo

The file adapter preserves every LIMS record and its source/version. Each
`receive_results` call replaces the complete snapshot; it never appends blindly.
Repeating an import does not duplicate records or findings. It does invalidate the
old package/QA decision and requires a new package revision; earlier evidence stays
in the event ledger. Duplicate result IDs within a batch are rejected as ambiguous;
different result IDs for one sample remain reviewable duplicate evidence.

Follow the [controller demo](controller.md), using `propose --fixture-plan` for the
hand-authored LIMS fixtures. `results --normal` yields six matches and no findings.
Omit `--normal` for the five deliberate anomalies. Both paths stop at `review_ready`
until a human supplies a QA decision. The existing independent QA gate rejects
anomalies even if a different review service omits its findings.

Generated plans require LIMS records referencing their actual stable IDs and revision;
the evaluator deliberately does not rewrite fixture IDs to fit a generated plan.
