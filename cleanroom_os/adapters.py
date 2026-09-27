"""Replaceable read-only input adapters for explicitly synthetic local scenarios."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Generic, Literal, Protocol, TypeVar

from pydantic import AwareDatetime, Field

from cleanroom_os.contracts import (
    Contract, Count, DailySchedule, Evidence, Identifier, LIMSResult,
    PlanningContext, Recipe, Room, SOPRequirements, SourceReference, Text, TimeWindow,
    parse_contract,
)

T = TypeVar("T")


class Fixture(Contract, Generic[T]):
    """A synthetic-data envelope around an existing exchange contract."""

    synthetic: Literal[True]
    data: T


class ExecutionAssumptions(Contract):
    """Explicit scenario timing and resource inputs, not planner defaults."""

    technician_id: Identifier
    technician_availability: TimeWindow
    starting_room_id: Identifier
    sample_minutes: Count
    setup_minutes_per_room: Count
    travel_minutes_between_rooms: Count
    collection_mode: Literal["sequential"]
    sources: Evidence


class Facility(Contract):
    """One mock facility and its explicit execution assumptions."""

    facility_id: Identifier
    name: Text
    rooms: list[Room] = Field(min_length=1)
    execution: ExecutionAssumptions


class HumanScheduleUpdate(Contract):
    """A synthetic human-supplied source update; loading it is not approval."""

    update_id: Identifier
    actor_id: Identifier
    role: Literal["manufacturing"]
    supplied_at: AwareDatetime
    previous_schedule_version: Text
    replacement_schedule_version: Text
    reason: Text
    sources: Evidence


class InputAdapter(Protocol):
    """Read inputs without coupling downstream code to storage or credentials."""

    def load_sop(self) -> SOPRequirements:
        """Retrieve the sampling procedure requirements."""
        ...

    def load_recipe(self) -> Recipe:
        """Retrieve the execution recipe."""
        ...

    def load_schedule(self, *, resolved: bool = False) -> DailySchedule:
        """Retrieve baseline or explicitly selected replacement constraints."""
        ...

    def load_lims(self, *, anomalies: bool = True) -> list[LIMSResult]:
        """Retrieve simulated measurements, retaining anomalous records."""
        ...

    def load_facility(self) -> Facility:
        """Retrieve room identities and scenario execution assumptions."""
        ...


class FileInputAdapter:
    """Load a local scenario directory; never fall back to live systems."""

    def __init__(self, directory: Path) -> None:
        """Bind the adapter to an explicit fixture directory."""
        self.directory = directory

    def _read(self, filename: str, model: type[T]) -> T:
        """Validate the envelope and data, preserving file/validation errors."""
        fixture = parse_contract(Fixture[model], (self.directory / filename).read_text(encoding="utf-8"))
        return fixture.data

    def load_sop(self) -> SOPRequirements:
        """Load the synthetic authoritative sampling SOP, not CR-SOP-001."""
        return self._read("sop.json", SOPRequirements)

    def load_recipe(self) -> Recipe:
        """Load the unchanged requirements shared by both variants."""
        return self._read("recipe.json", Recipe)

    def load_schedule(self, *, resolved: bool = False) -> DailySchedule:
        """Require explicit selection before loading the human replacement."""
        return self._read("schedule-resolved.json" if resolved else "schedule.json", DailySchedule)

    def load_lims(self, *, anomalies: bool = True) -> list[LIMSResult]:
        """Load result records without filtering duplicates or other anomalies."""
        return self._read("lims-anomalies.json" if anomalies else "lims-normal.json", list[LIMSResult])

    def load_facility(self) -> Facility:
        """Load facility and timing inputs."""
        return self._read("facility.json", Facility)

    def load_human_update(self) -> HumanScheduleUpdate:
        """Expose the provenance of the separate schedule update."""
        return self._read("human-update.json", HumanScheduleUpdate)

    def resolve_source(self, source: SourceReference) -> object:
        """Resolve a registered source/version and JSON Pointer into local evidence."""
        registry = json.loads((self.directory / "sources.json").read_text(encoding="utf-8"))
        matches = [r for r in registry["records"] if (r["kind"], r["source_id"], r["version"]) == (source.kind, source.source_id, source.version)]
        if len(matches) != 1:
            raise ValueError("Source must resolve to exactly one registered version")
        path = (self.directory / matches[0]["file"]).resolve()
        if not path.is_relative_to(self.directory.resolve()):
            raise ValueError("Source path escapes fixture directory")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not source.locator.startswith("/"):
            raise ValueError("Fixture evidence requires an absolute JSON Pointer")
        for token in source.locator[1:].split("/"):
            key = token.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not key.isdecimal():
                    raise ValueError("Invalid array pointer")
                value = value[int(key)]
            else:
                value = value[key]
        return value


def load_context(adapter: InputAdapter, *, resolved: bool = False) -> PlanningContext:
    """Assemble validated planning inputs from any conforming adapter."""
    return PlanningContext(sop=adapter.load_sop(), recipe=adapter.load_recipe(),
                           rooms=adapter.load_facility().rooms,
                           schedule=adapter.load_schedule(resolved=resolved))
