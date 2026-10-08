"""Tests for CleanRoomOS persistent runtime foundations."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from cleanroom_os.domain import (
    CleanroomInstanceCreate,
    TaskPayload,
    TaskRecordCreate,
)
from cleanroom_os.persistence import Database, RuntimeRepository, UnknownInstanceError
from cleanroom_os.server import create_app


class PersistenceRuntimeTests(unittest.TestCase):
    """Verify durable, instance-scoped repository behavior."""

    def setUp(self) -> None:
        """Create a unique file-backed SQLite database for persistence tests."""
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "runtime.sqlite"
        self.database_url = f"sqlite+pysqlite:///{self.database_path}"
        self.database = Database(self.database_url)
        self.database.initialize()
        self.repository = RuntimeRepository(self.database)

    def tearDown(self) -> None:
        """Dispose database resources and remove the temporary database."""
        self.database.dispose()
        self.tempdir.cleanup()

    def test_state_survives_repository_and_engine_restart(self) -> None:
        """Persisted instance/task state remains available after a restart."""
        instance = self.repository.create_instance(
            CleanroomInstanceCreate(id="lab-a", name="Lab A")
        )
        task = self.repository.create_record(
            instance.id,
            TaskRecordCreate(
                id="task-1",
                payload=TaskPayload(title="Collect sample", status="queued"),
            ),
        )
        self.database.dispose()

        restarted_database = Database(self.database_url)
        restarted_database.initialize()
        restarted_repository = RuntimeRepository(restarted_database)
        try:
            reloaded_instance = restarted_repository.get_instance("lab-a")
            reloaded_task = restarted_repository.get_record("lab-a", "task-1")
            self.assertIsNotNone(reloaded_instance)
            self.assertIsNotNone(reloaded_task)
            assert reloaded_task is not None
            self.assertEqual(task.payload, reloaded_task.payload)
        finally:
            restarted_database.dispose()

    def test_records_are_scoped_to_cleanroom_instance(self) -> None:
        """A record from one instance is not visible through another instance."""
        first = self.repository.create_instance(
            CleanroomInstanceCreate(id="lab-a", name="Lab A")
        )
        second = self.repository.create_instance(
            CleanroomInstanceCreate(id="lab-b", name="Lab B")
        )
        self.repository.create_record(
            first.id,
            TaskRecordCreate(
                id="task-a",
                payload=TaskPayload(title="A task", status="queued"),
            ),
        )
        self.assertEqual(1, len(self.repository.list_records(first.id)))
        self.assertEqual([], self.repository.list_records(second.id))
        self.assertIsNone(self.repository.get_record(second.id, "task-a"))

    def test_record_requires_existing_instance(self) -> None:
        """Operational state cannot exist outside an instance boundary."""
        with self.assertRaises(UnknownInstanceError):
            self.repository.create_record(
                "missing",
                TaskRecordCreate(
                    id="task-1",
                    payload=TaskPayload(title="A task", status="queued"),
                ),
            )


class PersistentApiTests(unittest.TestCase):
    """Verify the HTTP boundary persists and returns typed operational state."""

    def test_api_creates_instance_and_task(self) -> None:
        """Create and query instance-scoped state through FastAPI."""
        with tempfile.TemporaryDirectory() as tempdir:
            database_url = f"sqlite+pysqlite:///{Path(tempdir) / 'api.sqlite'}"
            with TestClient(create_app(database_url)) as client:
                instance_response = client.post(
                    "/v1/instances",
                    json={"id": "lab-a", "name": "Lab A", "attributes": {"iso_class": "ISO-7"}},
                )
                self.assertEqual(201, instance_response.status_code)

                task_response = client.post(
                    "/v1/instances/lab-a/records",
                    json={
                        "id": "task-1",
                        "kind": "task",
                        "payload": {"title": "Collect sample", "status": "queued"},
                    },
                )
                self.assertEqual(201, task_response.status_code)

                records_response = client.get("/v1/instances/lab-a/records?kind=task")
                self.assertEqual(200, records_response.status_code)
                records = records_response.json()
                self.assertEqual(1, len(records))
                self.assertEqual("task-1", records[0]["id"])
                self.assertEqual("task", records[0]["kind"])


if __name__ == "__main__":
    unittest.main()
