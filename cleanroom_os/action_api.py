"""HTTP routes for audited actions and operator-controlled policy revisions."""

from collections.abc import Callable

from fastapi import APIRouter, Depends, FastAPI, Query

from cleanroom_os.action_models import (
    ActionEventRead,
    ActionProposal,
    ActionRead,
    ActionSummary,
    ExecutionRequest,
    IncidentRead,
    InterventionRead,
    InterventionRequest,
    PolicyCreate,
    PolicyRead,
)
from cleanroom_os.action_runtime import ActionRuntime


def mount_action_routes(api: FastAPI, runtime: ActionRuntime, require_operator: Callable) -> None:
    router = APIRouter(prefix="/v1/instances/{instance_id}", tags=["Audited actions"])

    @router.post(
        "/policies",
        response_model=PolicyRead,
        status_code=201,
        dependencies=[Depends(require_operator)],
    )
    def create_policy(instance_id: str, payload: PolicyCreate) -> PolicyRead:
        return runtime.create_policy(instance_id, payload)

    @router.get("/policies", response_model=list[PolicyRead])
    def list_policies(
        instance_id: str, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)
    ) -> list[PolicyRead]:
        return runtime.list_policies(instance_id, limit, offset)

    @router.post("/actions", response_model=ActionRead, status_code=201)
    def propose_action(instance_id: str, payload: ActionProposal) -> ActionRead:
        return runtime.propose(instance_id, payload)

    @router.get("/actions", response_model=list[ActionSummary])
    def list_actions(
        instance_id: str,
        task_id: str | None = None,
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> list[ActionSummary]:
        return runtime.list_actions(instance_id, task_id, limit, offset)

    @router.get("/actions/{action_id}", response_model=ActionRead)
    def get_action(instance_id: str, action_id: str) -> ActionRead:
        return runtime.get_action(instance_id, action_id)

    @router.post("/actions/{action_id}/execute", response_model=ActionRead)
    def execute_action(instance_id: str, action_id: str, payload: ExecutionRequest) -> ActionRead:
        return runtime.execute(instance_id, action_id, payload)

    @router.post(
        "/actions/{action_id}/interventions",
        response_model=ActionRead,
        dependencies=[Depends(require_operator)],
    )
    def intervene(instance_id: str, action_id: str, payload: InterventionRequest) -> ActionRead:
        return runtime.intervene(instance_id, action_id, payload)

    @router.get("/action-events", response_model=list[ActionEventRead])
    def events(
        instance_id: str,
        action_id: str | None = None,
        task_id: str | None = None,
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> list[ActionEventRead]:
        return runtime.history(
            instance_id, action_id=action_id, task_id=task_id, limit=limit, offset=offset
        )

    @router.get("/policy-decisions", response_model=list[ActionEventRead])
    def decisions(
        instance_id: str,
        action_id: str | None = None,
        task_id: str | None = None,
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> list[ActionEventRead]:
        return runtime.history(
            instance_id,
            action_id=action_id,
            task_id=task_id,
            category="decisions",
            limit=limit,
            offset=offset,
        )

    @router.get("/incidents", response_model=list[IncidentRead])
    def incidents(
        instance_id: str,
        action_id: str | None = None,
        task_id: str | None = None,
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> list[IncidentRead]:
        return [
            event.incident
            for event in runtime.history(
                instance_id,
                action_id=action_id,
                task_id=task_id,
                category="incidents",
                limit=limit,
                offset=offset,
            )
            if event.incident is not None
        ]

    @router.get("/interventions", response_model=list[InterventionRead])
    def interventions(
        instance_id: str,
        action_id: str | None = None,
        task_id: str | None = None,
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> list[InterventionRead]:
        return [
            event.intervention
            for event in runtime.history(
                instance_id,
                action_id=action_id,
                task_id=task_id,
                category="interventions",
                limit=limit,
                offset=offset,
            )
            if event.intervention is not None
        ]

    api.include_router(router)
