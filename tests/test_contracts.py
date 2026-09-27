"""Exercise exchange boundaries and authority-preservation failures."""
import copy
import json
import unittest

from cleanroom_os.contracts import (
    ContractValidationError, HumanPlanningDecision, HumanQADecision, LIMSResult, PlanningContext,
    QAReviewPackage, SamplingPlan, TimeWindow, WorkflowTransition, parse_contract,
)


def source(kind: str, source_id: str) -> dict:
    """Build a synthetic source reference."""
    return dict(kind=kind, source_id=source_id, version="1", locator="section-1")


def window() -> dict:
    """Return a synthetic valid New York interval."""
    return dict(start="2026-09-27T09:00:00-04:00", end="2026-09-27T09:30:00-04:00", timezone="America/New_York")


def requirement(kind: str = "sop", source_id: str = "SOP-1") -> dict:
    """Return one sourced synthetic obligation."""
    return dict(requirement_id="REQ-1", room_id="A", sample_type="air", count=1,
                threshold=dict(value=10, unit="CFU/m3", operator="le", sources=[source(kind, source_id)]),
                sources=[source(kind, source_id)])


def plan() -> dict:
    """Return a complete plan with requirement and scheduling references."""
    return dict(plan_id="PLAN-1", revision=1, requirements=[requirement()], conflicts=[], samples=[dict(
        sample_id="SAMPLE-1", requirement_id="REQ-1", room_id="A", sample_type="air",
        window=window(), sequencing_reason="Available in the supplied window",
        sources=[source("sop", "SOP-1"), source("schedule", "SCH-1")])])


def context() -> dict:
    """Return consistent SOP, recipe, room, and schedule fixtures."""
    return dict(sop=dict(document_id="SOP-1", version="1", requirements=[requirement()]),
                recipe=dict(recipe_id="REC-1", version="1", requirements=[requirement("recipe", "REC-1")]),
                rooms=[dict(room_id="A", name="Room A", sources=[source("sop", "SOP-1")])],
                schedule=dict(schedule_id="SCH-1", version="1", shift=window(), occupancy=[],
                              availability=[dict(room_id="A", windows=[window()], sources=[source("schedule", "SCH-1")])]))


def result() -> dict:
    """Return a synthetic LIMS measurement linked to a plan revision."""
    return dict(result_id="LIMS-1", plan_id="PLAN-1", plan_revision=1, sample_id="SAMPLE-1",
                room_id="A", sample_type="air", collected_at=window()["start"], value=3,
                unit="CFU/m3", sources=[source("lims", "LIMS")])


class ContractTests(unittest.TestCase):
    """Verify rejection behavior as well as valid nested model serialization."""

    def reject(self, model: type, payload: object) -> None:
        """Assert a structured boundary failure with a nonempty error code."""
        with self.assertRaises(ContractValidationError) as caught:
            parse_contract(model, json.dumps(payload))
        self.assertTrue(caught.exception.issues[0].code)

    def test_round_trips(self) -> None:
        """All top-level exchanges preserve their nested content across JSON."""
        decision = dict(decision_id="D-1", package_id="PKG-1", package_revision=1,
                        plan_id="PLAN-1", plan_revision=1, actor_id="QA-1", role="qa",
                        decision="approve", rationale="Reviewed supplied evidence", decided_at=window()["end"],
                        sources=[source("human", "QA-1")])
        transition = dict(event_id="E-1", expected_state="validated", requested_state="plan_proposed",
                          plan_id="PLAN-1", plan_revision=1, actor_id="CTRL-1", actor_role="controller",
                          decision_id=None, occurred_at=window()["start"], sources=[source("schedule", "SCH-1")])
        package = dict(package_id="PKG-1", revision=1, plan=plan(), results=[result()], findings=[],
                       sources=[source("sop", "SOP-1")])
        for model, data in [(PlanningContext, context()), (SamplingPlan, plan()), (LIMSResult, result()),
                            (QAReviewPackage, package), (HumanQADecision, decision), (WorkflowTransition, transition)]:
            with self.subTest(model=model.__name__):
                parsed = parse_contract(model, json.dumps(data))
                self.assertEqual(parsed, parse_contract(model, parsed.model_dump_json()))
                self.assertIn("properties", model.model_json_schema())

    def test_bad_counts_and_identifiers(self) -> None:
        """Reject coercion, invalid counts, and malformed IDs."""
        for count in [0, -1, True, "2", 1.5, None]:
            data = plan()
            data["requirements"][0]["count"] = count
            self.reject(SamplingPlan, data)
        for identifier in ["", "bad id", "../path", "a" * 129]:
            data = plan()
            data["plan_id"] = identifier
            self.reject(SamplingPlan, data)

    def test_bad_windows(self) -> None:
        """Reject naive dates, reversed windows, and inconsistent time zones."""
        for change in [{"end": window()["start"]}, {"start": "2026-09-27T09:00:00"},
                       {"timezone": "Not/AZone"}, {"timezone": "UTC"}]:
            self.reject(TimeWindow, window() | change)

    def test_authority_conflict(self) -> None:
        """Neither authoritative input silently overrides the other."""
        for field, value in [("count", 2), ("threshold", dict(value=11, unit="CFU/m3", operator="le", sources=[source("recipe", "REC-1")]))]:
            data = context()
            data["recipe"]["requirements"][0][field] = value
            self.reject(PlanningContext, data)

    def test_unknowns_and_blocked_counts(self) -> None:
        """Unknown counts require a conflict; unknown thresholds remain unknown."""
        unknown = dict(status="unknown", reason="Not supplied", sources=[source("sop", "SOP-1")])
        data = plan()
        data["requirements"][0]["threshold"] = unknown
        parsed = parse_contract(SamplingPlan, json.dumps(data))
        self.assertEqual(parsed.requirements[0].threshold.status, "unknown")
        data["requirements"][0]["count"] = unknown
        self.reject(SamplingPlan, data)
        data["samples"] = []
        data["conflicts"] = [dict(conflict_id="C-1", requirement_id="REQ-1", room_id="A", reason="Unknown count", sources=[source("sop", "SOP-1")])]
        parse_contract(SamplingPlan, json.dumps(data))
        data["requirements"][0]["count"] = 3
        self.assertEqual(parse_contract(SamplingPlan, json.dumps(data)).requirements[0].count, 3)
        data["conflicts"] = []
        self.reject(SamplingPlan, data)

    def test_provenance(self) -> None:
        """Samples retain exact requirement citations and operational evidence."""
        parsed = parse_contract(SamplingPlan, json.dumps(plan()))
        self.assertIn(parsed.requirements[0].sources[0], parsed.samples[0].sources)
        data = plan()
        data["samples"][0]["sources"] = [source("schedule", "SCH-1")]
        self.reject(SamplingPlan, data)
        data = context()
        data["sop"]["version"] = "2"
        self.reject(PlanningContext, data)
        data = result()
        data["sources"] = [source("schedule", "SCH-1")]
        self.reject(LIMSResult, data)

    def test_identity_and_required_fields(self) -> None:
        """Reject orphan identities, duplicate samples, and missing required data."""
        for key, value in [("room_id", "B"), ("sample_type", "surface"), ("requirement_id", "absent")]:
            data = plan()
            data["samples"][0][key] = value
            self.reject(SamplingPlan, data)
        data = plan()
        data["samples"].append(copy.deepcopy(data["samples"][0]))
        self.reject(SamplingPlan, data)
        data = result()
        del data["plan_revision"]
        self.reject(LIMSResult, data)
        data = result() | {"unit": "invented"}
        self.reject(LIMSResult, data)
        data = context()
        data["rooms"][0]["room_id"] = "B"
        self.reject(PlanningContext, data)

    def test_human_decision_roles(self) -> None:
        """Operational permission cannot become final QA authority."""
        data = dict(decision_id="D-1", plan_id="PLAN-1", plan_revision=1,
                    actor_id="M-1", role="manufacturing", decision="allow",
                    rationale="Operational review", decided_at=window()["end"],
                    sources=[source("human", "M-1")])
        parse_contract(HumanPlanningDecision, json.dumps(data))
        data.update(package_id="PKG-1", package_revision=1, decision="approve")
        self.reject(HumanQADecision, data)
        data["role"] = "qa"
        parse_contract(HumanQADecision, json.dumps(data))

    def test_prose_and_extra_fields(self) -> None:
        """Agent prose and unsupported fields never become valid exchanges."""
        for payload in ['The plan is good.', '"The plan is good."', '{}', '[]']:
            with self.assertRaises(ContractValidationError):
                parse_contract(SamplingPlan, payload)
        self.reject(SamplingPlan, plan() | {"approved": True})


if __name__ == "__main__":
    unittest.main()
