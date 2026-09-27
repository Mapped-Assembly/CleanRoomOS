"""Small agent response contracts; none contains a state transition or decision."""
from typing import Generic, TypeVar

from cleanroom_os.contracts import (
    Contract, Evidence, Identifier, PlanningConflict, PlannedSample,
    Recipe, SOPRequirements, Text, Unknown,
)

T = TypeVar('T', bound=Contract)


class AgentResponse(Contract, Generic[T]):
    """Agents must explicitly report assumptions and unresolved source gaps."""

    output: T
    assumptions: list[Unknown]
    gaps: list[Unknown]


class RequirementsDraft(Contract):
    """Applicable requirements extracted from the supplied structured source documents."""

    sop: SOPRequirements
    recipe: Recipe


class ScheduleDraft(Contract):
    """A schedule proposal cannot redefine requirements, rooms, or availability."""

    samples: list[PlannedSample]
    conflicts: list[PlanningConflict]


class EvidenceNote(Contract):
    """Model-authored explanation; never an authoritative rule or human decision."""

    text: Text
    sources: Evidence


class FindingExplanation(EvidenceNote):
    finding_id: Identifier


class ReviewDraft(Contract):
    """Explanations accompany controller-owned findings; they cannot replace them."""

    summary: EvidenceNote
    explanations: list[FindingExplanation]


def source_gaps(value: Contract) -> list[Unknown]:
    """Collect explicit Unknown facts, preserving exact reasons and citations."""
    found = []

    def walk(item):
        if isinstance(item, dict):
            if item.get('status') == 'unknown':
                gap = Unknown.model_validate(item)
                if gap not in found:
                    found.append(gap)
            else:
                for child in item.values():
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
    walk(value.model_dump())
    return found


def validate_response(response: AgentResponse, expected_gaps: list[Unknown]) -> None:
    """Unprovided assumptions require human source updates, never silent execution."""
    if response.assumptions:
        raise ValueError('Agent supplied unverified assumptions; human source resolution required')
    if len(response.gaps) != len(expected_gaps) or any(g not in response.gaps for g in expected_gaps):
        raise ValueError('Agent changed or omitted explicit source gaps')
