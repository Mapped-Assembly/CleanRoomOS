"""Action lifecycle acceptance tests, run against SQLite and PostgreSQL in CI."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from cleanroom_os.action_models import ActionProposal, ExecutionRequest, ExecutionResult
from cleanroom_os.action_runtime import ActionRuntime
from cleanroom_os.persistence import Database
from cleanroom_os.server import create_app


class RecordingExecutor:
    def __init__(self):
        self.calls = []
        self.callback = None

    def execute(self, action_id, proposal):
        self.calls.append(action_id)
        if self.callback:
            self.callback(action_id)
        return ExecutionResult(
            outcome="completed", summary="Test adapter completed", outputs={"sample": "synthetic"}
        )


class ActionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.url = (
            os.getenv("CLEANROOM_TEST_DATABASE_URL")
            or f"sqlite+pysqlite:///{Path(self.tempdir.name) / 'actions.sqlite'}"
        )
        self.now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        self.headers = {"Authorization": "Bearer test-operator"}
        self.executor = RecordingExecutor()
        self._open_client()
        suffix = uuid4().hex
        self.instance, self.actor, self.task = f"lab-{suffix}", f"robot-{suffix}", f"task-{suffix}"
        self.prefix = f"/v1/instances/{self.instance}"
        self.assertEqual(
            201,
            self.client.post(
                "/v1/instances", json={"id": self.instance, "name": "Lab A"}
            ).status_code,
        )
        self.record("agent", {"name": "Robot A", "status": "ready"}, self.actor)
        self.record(
            "task",
            {"title": "Collect sample", "status": "queued", "assigned_agent_id": self.actor},
            self.task,
        )
        self.policy()
        self.maintenance()
        self.calibration()
        self.contamination()

    def _open_client(self):
        self.app = create_app(self.url, operator_token="test-operator", executor=self.executor)
        self.app.state.actions.clock = lambda: self.now
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tempdir.cleanup()

    def evidence(self):
        return [
            {
                "source": "synthetic-sensor",
                "reference": "fixture://zone-a/reading",
                "captured_at": (self.now - timedelta(minutes=1)).isoformat(),
                "details": {"fixture": True},
            }
        ]

    def record(self, kind, payload, record_id=None):
        data = {"kind": kind, "payload": payload}
        if record_id:
            data["id"] = record_id
        response = self.client.post(f"{self.prefix}/records", json=data, headers=self.headers)
        self.assertEqual(201, response.status_code, response.text)
        return response.json()

    def policy(self, revision=1, **changes):
        data = {
            "policy_id": "sampling",
            "revision": revision,
            "sop_refs": [{"document_id": "SOP-014", "version": "2.1", "locator": "section 4.2"}],
            "allowed_zones": ["zone-a"],
            "overridable_checks": ["risk_review"],
        }
        data.update(changes)
        response = self.client.post(f"{self.prefix}/policies", json=data, headers=self.headers)
        self.assertEqual(201, response.status_code, response.text)
        return response.json()

    def maintenance(self, **changes):
        data = {
            "equipment_id": "pipette-1",
            "status": "current",
            "performed_at": (self.now - timedelta(days=1)).isoformat(),
            "due_at": (self.now + timedelta(days=1)).isoformat(),
        }
        data.update(changes)
        return self.record("maintenance", data)

    def calibration(self, **changes):
        data = {
            "equipment_id": "pipette-1",
            "status": "valid",
            "calibrated_at": (self.now - timedelta(days=1)).isoformat(),
            "due_at": (self.now + timedelta(days=1)).isoformat(),
        }
        data.update(changes)
        return self.record("calibration", data)

    def contamination(self, **changes):
        data = {
            "zone_id": "zone-a",
            "status": "clean",
            "observed_at": self.now.isoformat(),
            "expires_at": (self.now + timedelta(days=2)).isoformat(),
            "evidence": self.evidence(),
        }
        data.update(changes)
        return self.record("contamination", data)

    def proposal(self, **changes):
        data = {
            "actor_id": self.actor,
            "task_id": self.task,
            "name": "collect_surface_sample",
            "target_id": "surface-7",
            "equipment_id": "pipette-1",
            "zone_id": "zone-a",
            "policy_id": "sampling",
            "reason": "Scheduled sample required by SOP-014",
            "risk_level": "low",
            "inputs": {"sample_count": 1},
            "evidence": self.evidence(),
        }
        data.update(changes)
        return data

    def propose(self, **changes):
        response = self.client.post(f"{self.prefix}/actions", json=self.proposal(**changes))
        self.assertEqual(201, response.status_code, response.text)
        return response.json()

    def execute(self, action, **changes):
        data = {"actor_id": self.actor, "reason": "Execute the scheduled sample"}
        data.update(changes)
        response = self.client.post(f"{self.prefix}/actions/{action['id']}/execute", json=data)
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def intervene(self, action, **changes):
        data = {
            "operator_id": "operator-1",
            "decision_id": action["decision_id"],
            "kind": "override",
            "reason": "Reviewed the bounded escalation and supporting evidence",
            "evidence": self.evidence(),
        }
        data.update(changes)
        response = self.client.post(
            f"{self.prefix}/actions/{action['id']}/interventions", json=data, headers=self.headers
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def test_nominal_records_decision_before_dispatch_and_complete_provenance(self):
        action = self.propose()
        self.assertEqual("ready", action["state"])
        self.executor.callback = lambda action_id: self.assertEqual(
            "execution_started",
            self.app.state.actions.get_action(self.instance, action_id).events[-1].kind,
        )
        completed = self.execute(action)
        self.assertEqual("completed", completed["state"])
        self.assertEqual(
            ["proposed", "execution_started", "completed"], [e["kind"] for e in completed["events"]]
        )
        self.assertEqual([1, 2, 3], [e["sequence"] for e in completed["events"]])
        self.assertEqual(action["events"][0], completed["events"][0])
        event = completed["events"][1]
        self.assertEqual(
            ActionProposal.model_validate(self.proposal()),
            ActionProposal.model_validate(event["proposal"]),
        )
        self.assertEqual("2.1", event["evaluation"]["context"]["policy"]["sop_refs"][0]["version"])
        self.assertEqual(1, event["evaluation"]["context"]["policy"]["revision"])
        self.assertEqual(
            {"actor", "task", "maintenance", "calibration", "contamination"},
            set(event["evaluation"]["context"]["records"]),
        )
        self.assertEqual("allow", event["evaluation"]["effective_decision"])
        self.assertEqual({"sample": "synthetic"}, completed["events"][-1]["result"]["outputs"])
        self.assertEqual([action["id"]], self.executor.calls)

    def test_contamination_blocks_and_links_queryable_incident(self):
        self.contamination(status="contaminated")
        action = self.propose()
        self.assertEqual("blocked", action["state"])
        self.assertEqual("blocked", self.execute(action)["state"])
        self.assertEqual([], self.executor.calls)
        incidents = self.client.get(
            f"{self.prefix}/incidents", params={"task_id": self.task}
        ).json()
        self.assertEqual(action["id"], incidents[0]["action_id"])
        self.assertEqual(action["events"][0]["id"], incidents[0]["event_id"])
        self.assertTrue(incidents[0]["requires_operator"])
        decisions = self.client.get(
            f"{self.prefix}/policy-decisions", params={"action_id": action["id"]}
        ).json()
        self.assertTrue(all(e["evaluation"]["decision"] == "block" for e in decisions))

    def test_unknown_stale_and_future_contamination_escalates(self):
        for changes in (
            {"status": "unknown"},
            {"status": "suspected"},
            {"expires_at": self.now.isoformat()},
            {"observed_at": (self.now + timedelta(minutes=1)).isoformat()},
        ):
            with self.subTest(changes=changes):
                self.contamination(**changes)
                action = self.propose()
                self.assertEqual("escalated", action["state"])
                self.assertEqual("escalated", self.execute(action)["state"])
        self.assertEqual([], self.executor.calls)

    def test_maintenance_and_calibration_gates(self):
        for kind, changes, expected in (
            ("maintenance", {"status": "overdue"}, "blocked"),
            ("maintenance", {"status": "out_of_service"}, "blocked"),
            ("maintenance", {"faults": ["drive fault"]}, "blocked"),
            ("maintenance", {"due_at": self.now.isoformat()}, "blocked"),
            ("maintenance", {"status": "due"}, "escalated"),
            ("maintenance", {"due_at": None}, "escalated"),
            ("maintenance", {"performed_at": "2025-12-31T12:00:00"}, "escalated"),
            ("calibration", {"status": "expired"}, "blocked"),
            ("calibration", {"due_at": self.now.isoformat()}, "blocked"),
            ("calibration", {"status": "unknown"}, "escalated"),
            ("calibration", {"status": "due"}, "escalated"),
            ("calibration", {"due_at": None}, "escalated"),
            (
                "calibration",
                {"calibrated_at": (self.now + timedelta(hours=1)).isoformat()},
                "escalated",
            ),
        ):
            with self.subTest(kind=kind, changes=changes):
                self.maintenance()
                self.calibration()
                getattr(self, kind)(**changes)
                action = self.propose()
                self.assertEqual(expected, action["state"])
                self.execute(action)
        self.assertEqual([], self.executor.calls)

    def test_missing_context_and_forbidden_zone_persist_attempts(self):
        for changes, expected in (
            ({"actor_id": "missing"}, "blocked"),
            ({"task_id": "missing"}, "blocked"),
            ({"policy_id": "missing"}, "blocked"),
            ({"zone_id": "forbidden"}, "blocked"),
            ({"equipment_id": "missing"}, "escalated"),
            ({"risk_level": "high"}, "blocked"),
        ):
            with self.subTest(changes=changes):
                action = self.propose(**changes)
                self.assertEqual(expected, action["state"])
                self.assertEqual(1, len(action["events"]))
                self.assertEqual(
                    action["id"],
                    self.client.get(f"{self.prefix}/actions/{action['id']}").json()["id"],
                )
        self.assertEqual(6, len(self.client.get(f"{self.prefix}/actions").json()))

    def test_policy_permitted_override_preserves_original_and_operator_evidence(self):
        action = self.propose(risk_level="medium")
        overridden = self.intervene(action)
        self.assertEqual("ready", overridden["state"])
        intervention = overridden["events"][-1]["intervention"]
        self.assertTrue(intervention["accepted"])
        self.assertEqual(action["decision_id"], intervention["decision_id"])
        completed = self.execute(overridden)
        self.assertEqual("completed", completed["state"])
        self.assertEqual(action["events"][0], completed["events"][0])
        evaluation = completed["events"][-2]["evaluation"]
        self.assertEqual("escalate", evaluation["decision"])
        self.assertEqual("allow", evaluation["effective_decision"])
        self.assertEqual(intervention["id"], evaluation["overridden_by"])
        saved = self.client.get(
            f"{self.prefix}/interventions", params={"action_id": action["id"]}
        ).json()[0]
        self.assertEqual(self.task, saved["task_id"])
        self.assertEqual(intervention, saved)

    def test_suspected_contamination_requires_explicit_policy_override_permission(self):
        self.contamination(status="suspected")
        action = self.propose()
        denied = self.intervene(action)
        self.assertFalse(denied["events"][-1]["intervention"]["accepted"])
        self.policy(2, overridable_checks=["contamination_suspected"])
        action = self.propose()
        accepted = self.intervene(action)
        self.assertTrue(accepted["events"][-1]["intervention"]["accepted"])
        self.assertEqual("completed", self.execute(accepted)["state"])

    def test_hard_blocks_and_missing_required_state_cannot_be_overridden(self):
        for changes in (
            {"risk_level": "high"},
            {"equipment_id": "unknown", "risk_level": "medium"},
        ):
            with self.subTest(changes=changes):
                action = self.propose(**changes)
                result = self.intervene(action)
                self.assertFalse(result["events"][-1]["intervention"]["accepted"])
                self.execute(result)
        self.assertEqual([], self.executor.calls)

    def test_stale_operator_review_and_changed_policy_are_rejected(self):
        action = self.propose(risk_level="medium")
        stale = self.intervene(action, decision_id="unreviewed-event")
        self.assertFalse(stale["events"][-1]["intervention"]["accepted"])
        self.policy(2)
        changed = self.intervene(action)
        self.assertFalse(changed["events"][-1]["intervention"]["accepted"])
        self.assertNotEqual(action["decision_id"], changed["decision_id"])
        self.assertTrue(self.intervene(changed)["events"][-1]["intervention"]["accepted"])

    def test_override_is_invalidated_by_changed_evidence_before_execution(self):
        action = self.intervene(self.propose(risk_level="medium"))
        self.calibration()  # Even equivalent readings have a new source record ID.
        result = self.execute(action)
        self.assertEqual("escalated", result["state"])
        self.assertIsNone(result["events"][-1]["evaluation"]["overridden_by"])
        self.assertEqual([], self.executor.calls)

    def test_execution_rechecks_time_and_current_policy(self):
        action = self.propose()
        self.now += timedelta(days=1)
        expired = self.execute(action)
        self.assertEqual("blocked", expired["state"])
        self.assertIn(
            "calibration_overdue",
            [c["code"] for c in expired["events"][-1]["evaluation"]["checks"]],
        )
        self.maintenance()
        self.calibration()
        action = self.propose()
        self.policy(2, allowed_zones=["zone-b"])
        self.assertEqual("blocked", self.execute(action)["state"])
        self.assertEqual([], self.executor.calls)

    def test_operator_reject_and_repeated_execution_keep_history(self):
        rejected = self.intervene(self.propose(), kind="reject")
        self.assertEqual("rejected", self.execute(rejected)["state"])
        action = self.execute(self.propose())
        repeated = self.execute(action)
        self.assertEqual("completed", repeated["state"])
        self.assertEqual("execution_rejected", repeated["events"][-1]["kind"])
        self.assertEqual(action["events"], repeated["events"][:-1])
        self.assertEqual(1, len(self.executor.calls))

    def test_wrong_execution_actor_is_audited_without_dispatch(self):
        result = self.execute(self.propose(), actor_id="different-robot")
        self.assertEqual("blocked", result["state"])
        self.assertEqual("different-robot", result["events"][-1]["actor_id"])
        self.assertEqual([], self.executor.calls)

    def test_failed_adapter_is_sanitized_and_incident_is_persisted(self):
        def fail(action_id):
            raise RuntimeError("private adapter credential")

        self.executor.callback = fail
        action = self.execute(self.propose())
        self.assertEqual("failed", action["state"])
        self.assertIsNotNone(action["events"][-1]["incident"])
        self.assertNotIn("private adapter credential", json.dumps(action))
        self.execute(action)
        self.assertEqual(1, len(self.executor.calls))

    def test_audit_commit_failure_rolls_back_and_prevents_dispatch(self):
        action = self.propose()
        with patch(
            "sqlalchemy.orm.Session.commit", side_effect=RuntimeError("test commit failure")
        ):
            with self.assertRaisesRegex(RuntimeError, "test commit failure"):
                self.app.state.actions.execute(
                    self.instance,
                    action["id"],
                    ExecutionRequest(actor_id=self.actor, reason="test"),
                )
        self.assertEqual([], self.executor.calls)
        saved = self.client.get(f"{self.prefix}/actions/{action['id']}").json()
        self.assertEqual(action, saved)

    def test_two_workers_cannot_dispatch_the_same_action_twice(self):
        action = self.propose()
        entered, release = threading.Event(), threading.Event()

        def hold(action_id):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release executor")

        self.executor.callback = hold
        second_db = Database(self.url)
        second = ActionRuntime(second_db, self.executor, clock=lambda: self.now)
        request = ExecutionRequest(actor_id=self.actor, reason="Concurrent request")
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                future = pool.submit(
                    self.app.state.actions.execute, self.instance, action["id"], request
                )
                try:
                    self.assertTrue(entered.wait(5))
                    duplicate = second.execute(self.instance, action["id"], request)
                    self.assertEqual("executing", duplicate.state)
                    self.assertEqual("execution_rejected", duplicate.events[-1].kind)
                    self.assertEqual([action["id"]], self.executor.calls)
                finally:
                    release.set()
                self.assertEqual("completed", future.result(timeout=5).state)
        finally:
            second_db.dispose()
        saved = self.client.get(f"{self.prefix}/actions/{action['id']}").json()
        self.assertEqual([1, 2, 3, 4], [e["sequence"] for e in saved["events"]])

    def test_history_survives_restart_and_state_updates_do_not_rewrite_evidence(self):
        action = self.execute(self.propose())
        self.maintenance(status="overdue")
        self.client.__exit__(None, None, None)
        self._open_client()
        self.assertEqual(action, self.client.get(f"{self.prefix}/actions/{action['id']}").json())
        events = self.client.get(
            f"{self.prefix}/action-events", params={"action_id": action["id"]}
        ).json()
        self.assertEqual(action["events"], events)

    def test_interrupted_dispatch_remains_durable_and_is_not_reexecuted(self):
        action = self.propose()

        def interrupted(action_id):
            raise SystemExit("simulated worker termination")

        self.executor.callback = interrupted
        request = ExecutionRequest(actor_id=self.actor, reason="Test interrupted dispatch")
        with self.assertRaises(SystemExit):
            self.app.state.actions.execute(self.instance, action["id"], request)
        restarted_db = Database(self.url)
        try:
            restarted = ActionRuntime(restarted_db, self.executor, clock=lambda: self.now)
            saved = restarted.execute(self.instance, action["id"], request)
            self.assertEqual("executing", saved.state)
            self.assertEqual("execution_rejected", saved.events[-1].kind)
            self.assertEqual([action["id"]], self.executor.calls)
        finally:
            restarted_db.dispose()

    def test_instance_boundaries_cover_actions_evidence_and_histories(self):
        action = self.propose(risk_level="medium")
        other = "other-" + uuid4().hex
        self.client.post("/v1/instances", json={"id": other, "name": "Other lab"})
        other_prefix = f"/v1/instances/{other}"
        for suffix in (f"/actions/{action['id']}",):
            self.assertEqual(404, self.client.get(other_prefix + suffix).status_code)
        self.assertEqual(
            404,
            self.client.post(
                f"{other_prefix}/actions/{action['id']}/execute",
                json={"actor_id": self.actor, "reason": "Wrong instance"},
            ).status_code,
        )
        for resource in (
            "actions",
            "action-events",
            "incidents",
            "interventions",
            "policy-decisions",
            "policies",
        ):
            self.assertEqual([], self.client.get(f"{other_prefix}/{resource}").json())
        foreign = self.client.post(f"{other_prefix}/actions", json=self.proposal()).json()
        self.assertEqual("blocked", foreign["state"])
        self.assertTrue(
            all(
                value is None
                for value in foreign["events"][0]["evaluation"]["context"]["records"].values()
            )
        )

    def test_operator_auth_protects_policy_state_and_interventions(self):
        action = self.propose(risk_level="medium")
        requests = [
            (
                "/policies",
                {
                    "policy_id": "new",
                    "revision": 1,
                    "sop_refs": [{"document_id": "SOP", "version": "1", "locator": "1"}],
                    "allowed_zones": ["a"],
                },
            ),
            (
                "/records",
                {
                    "kind": "maintenance",
                    "payload": {"equipment_id": "pipette-1", "status": "current"},
                },
            ),
            (
                "/records",
                {
                    "kind": "calibration",
                    "payload": {"equipment_id": "pipette-1", "status": "valid"},
                },
            ),
            (
                "/records",
                {
                    "kind": "contamination",
                    "payload": {
                        "zone_id": "zone-a",
                        "status": "clean",
                        "observed_at": self.now.isoformat(),
                        "expires_at": (self.now + timedelta(hours=1)).isoformat(),
                        "evidence": self.evidence(),
                    },
                },
            ),
            (
                f"/actions/{action['id']}/interventions",
                {
                    "operator_id": "claimed-human",
                    "decision_id": action["decision_id"],
                    "kind": "override",
                    "reason": "Review",
                    "evidence": self.evidence(),
                },
            ),
        ]
        for suffix, data in requests:
            with self.subTest(suffix=suffix):
                self.assertEqual(401, self.client.post(self.prefix + suffix, json=data).status_code)
                self.assertEqual(
                    401,
                    self.client.post(
                        self.prefix + suffix, json=data, headers={"Authorization": "Bearer wrong"}
                    ).status_code,
                )
        unconfigured = create_app(self.url, operator_token="")
        with TestClient(unconfigured) as client:
            self.assertEqual(
                503,
                client.post(
                    self.prefix + requests[0][0], json=requests[0][1], headers=self.headers
                ).status_code,
            )

    def test_generic_records_cannot_forge_actions_decisions_incidents_or_overrides(self):
        for kind, payload in (
            ("action", {"name": "unaudited", "status": "completed"}),
            ("risk_decision", {"risk_level": "low", "decision": "allow", "reason": "forged"}),
            ("incident", {"summary": "forged", "severity": "low", "status": "resolved"}),
            (
                "operator_intervention",
                {"operator_id": "fake", "action": "override", "reason": "forged"},
            ),
        ):
            with self.subTest(kind=kind):
                self.assertEqual(
                    409,
                    self.client.post(
                        f"{self.prefix}/records", json={"kind": kind, "payload": payload}
                    ).status_code,
                )
        self.assertEqual([], self.client.get(f"{self.prefix}/actions").json())

    def test_policy_revisions_are_immutable_and_client_cannot_choose_old_revision(self):
        self.policy(2, allowed_zones=["zone-b"])
        response = self.client.post(
            f"{self.prefix}/policies",
            json={
                "policy_id": "sampling",
                "revision": 1,
                "sop_refs": [{"document_id": "SOP", "version": "old", "locator": "1"}],
                "allowed_zones": ["zone-a"],
            },
            headers=self.headers,
        )
        self.assertEqual(409, response.status_code)
        self.assertEqual("blocked", self.propose()["state"])
        self.assertEqual(
            422,
            self.client.post(
                f"{self.prefix}/actions", json=self.proposal(policy_revision=1)
            ).status_code,
        )
        self.assertEqual(
            [1, 2], [p["revision"] for p in self.client.get(f"{self.prefix}/policies").json()]
        )

    def test_unconfigured_executor_fails_closed_and_history_is_paginated(self):
        self.app.state.actions.executor = None
        action = self.execute(self.propose())
        self.assertEqual("blocked", action["state"])
        self.assertIn(
            "executor_missing", [c["code"] for c in action["events"][-1]["evaluation"]["checks"]]
        )
        first = self.client.get(
            f"{self.prefix}/action-events", params={"action_id": action["id"], "limit": 1}
        ).json()
        second = self.client.get(
            f"{self.prefix}/action-events",
            params={"action_id": action["id"], "limit": 1, "offset": 1},
        ).json()
        self.assertEqual(action["events"], first + second)
        self.assertEqual(422, self.client.get(f"{self.prefix}/actions?limit=0").status_code)
        self.assertEqual([], self.executor.calls)

    def test_missing_zone_observation_and_future_operator_evidence_do_not_authorize(self):
        self.policy(2, allowed_zones=["zone-a", "unobserved"])
        missing = self.propose(zone_id="unobserved")
        self.assertEqual("escalated", missing["state"])
        self.assertIsNone(missing["events"][0]["evaluation"]["context"]["records"]["contamination"])
        action = self.propose(risk_level="medium")
        future = self.evidence()
        future[0]["captured_at"] = (self.now + timedelta(minutes=1)).isoformat()
        rejected = self.intervene(action, evidence=future)
        self.assertFalse(rejected["events"][-1]["intervention"]["accepted"])
        self.assertEqual("escalated", self.execute(rejected)["state"])

    def test_simulated_adapter_is_explicit_and_does_not_claim_physical_execution(self):
        with patch.dict(os.environ, {"CLEANROOM_ACTION_EXECUTOR": "simulation"}):
            app = create_app(self.url, operator_token="test-operator")
        app.state.actions.clock = lambda: self.now
        with TestClient(app) as client:
            action = client.post(f"{self.prefix}/actions", json=self.proposal()).json()
            result = client.post(
                f"{self.prefix}/actions/{action['id']}/execute",
                json={"actor_id": self.actor, "reason": "Explicit simulation"},
            ).json()
            self.assertEqual("completed", result["state"])
            self.assertEqual("simulation", result["events"][-1]["result"]["outputs"]["mode"])
            self.assertIn("no equipment was actuated", result["events"][-1]["result"]["summary"])


if __name__ == "__main__":
    unittest.main()
