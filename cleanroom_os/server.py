"""FastAPI service exposing the persistent CleanRoomOS runtime."""
from __future__ import annotations

import hmac
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from cleanroom_os.action_api import mount_action_routes
from cleanroom_os.action_runtime import ActionExecutor, ActionRuntime, SimulatedExecutor, UnknownActionError
from cleanroom_os.domain import (
    CleanroomInstanceCreate,
    CleanroomInstanceRead,
    OperationalRecordCreate,
    OperationalRecordRead,
    RecordKind,
)
from cleanroom_os.persistence import (
    Database,
    AuditedActionRequiredError,
    DuplicateIdentifierError,
    RuntimeRepository,
    PersistenceError,
    UnknownInstanceError,
)

DEFAULT_DATABASE_URL = "postgresql+psycopg://cleanroom:cleanroom@localhost:5432/cleanroom"


def database_url_from_environment() -> str:
    """Return the runtime database URL, defaulting to local PostgreSQL."""
    return os.getenv("CLEANROOM_DATABASE_URL", DEFAULT_DATABASE_URL)


def create_app(database_url: str | None = None, *, operator_token: str | None = None,
               executor: ActionExecutor | None = None) -> FastAPI:
    """Create a persistent CleanRoomOS API bound to one database backend."""
    database = Database(database_url or database_url_from_environment())
    repository = RuntimeRepository(database)
    operator_token = operator_token if operator_token is not None else os.getenv("CLEANROOM_OPERATOR_TOKEN", "")
    adapter_mode = os.getenv("CLEANROOM_ACTION_EXECUTOR", "disabled")
    if adapter_mode not in {"disabled", "simulation"}:
        raise ValueError("CLEANROOM_ACTION_EXECUTOR must be disabled or simulation")
    if executor is None and adapter_mode == "simulation":
        executor = SimulatedExecutor()
    actions = ActionRuntime(database, executor)

    def require_operator(authorization: str | None = Header(default=None)) -> None:
        if not operator_token:
            raise HTTPException(status_code=503, detail="Operator access is not configured")
        supplied = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        if not hmac.compare_digest(supplied.encode(), operator_token.encode()):
            raise HTTPException(status_code=401, detail="Operator credential required", headers={"WWW-Authenticate": "Bearer"})

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Initialize deterministic schema state and close database resources."""
        del app
        database.initialize()
        try:
            yield
        finally:
            database.dispose()

    api = FastAPI(
        title="CleanRoomOS Persistent Runtime",
        version="0.1.0",
        lifespan=lifespan,
    )
    api.state.database = database
    api.state.repository = repository
    api.state.actions = actions

    async def missing_resource(request: Request, exc: PersistenceError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    async def conflicting_resource(request: Request, exc: PersistenceError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    api.add_exception_handler(UnknownInstanceError, missing_resource)
    api.add_exception_handler(UnknownActionError, missing_resource)
    api.add_exception_handler(DuplicateIdentifierError, conflicting_resource)
    api.add_exception_handler(AuditedActionRequiredError, conflicting_resource)
    mount_action_routes(api, actions, require_operator)

    @api.get("/healthz")
    def health() -> dict[str, str]:
        """Report persistence connectivity for deployment health checks."""
        database.ping()
        return {"status": "ok"}

    @api.post(
        "/v1/instances",
        response_model=CleanroomInstanceRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_instance(payload: CleanroomInstanceCreate) -> CleanroomInstanceRead:
        """Create a durable cleanroom/environment instance."""
        try:
            return repository.create_instance(payload)
        except DuplicateIdentifierError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @api.get("/v1/instances", response_model=list[CleanroomInstanceRead])
    def list_instances() -> list[CleanroomInstanceRead]:
        """List durable cleanroom/environment instances."""
        return repository.list_instances()

    @api.get("/v1/instances/{instance_id}", response_model=CleanroomInstanceRead)
    def get_instance(instance_id: str) -> CleanroomInstanceRead:
        """Fetch one cleanroom instance by stable ID."""
        instance = repository.get_instance(instance_id)
        if instance is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Instance not found")
        return instance

    @api.post(
        "/v1/instances/{instance_id}/records",
        response_model=OperationalRecordRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_record(
        instance_id: str,
        payload: OperationalRecordCreate,
        authorization: str | None = Header(default=None),
    ) -> OperationalRecordRead:
        """Create one typed operational record inside an instance."""
        if payload.kind in {"maintenance", "calibration", "contamination", "sop_reference"}:
            require_operator(authorization)
        try:
            return repository.create_record(instance_id, payload)
        except UnknownInstanceError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except DuplicateIdentifierError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @api.get(
        "/v1/instances/{instance_id}/records",
        response_model=list[OperationalRecordRead],
    )
    def list_records(
        instance_id: str,
        kind: RecordKind | None = Query(default=None),
    ) -> list[OperationalRecordRead]:
        """List ordered operational records for one instance."""
        try:
            return repository.list_records(instance_id, kind=kind)
        except UnknownInstanceError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @api.get(
        "/v1/instances/{instance_id}/records/{record_id}",
        response_model=OperationalRecordRead,
    )
    def get_record(instance_id: str, record_id: str) -> OperationalRecordRead:
        """Fetch one operational record scoped to its instance."""
        record = repository.get_record(instance_id, record_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")
        return record

    return api


def main() -> None:
    """Run the persistent API with Uvicorn using environment configuration."""
    import uvicorn

    host = os.getenv("CLEANROOM_HOST", "127.0.0.1")
    port = int(os.getenv("CLEANROOM_PORT", "8000"))
    uvicorn.run("cleanroom_os.server:app", host=host, port=port, reload=False)


app = create_app()


if __name__ == "__main__":
    main()
