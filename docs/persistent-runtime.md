# Persistent runtime

Issue #22 introduces a database-backed service boundary alongside the existing sampling POC controller. The existing `Controller` and its SQLite workflow remain unchanged; new operational state should use this runtime API.

## Database

Production runtime state is backed by PostgreSQL through SQLAlchemy. Set:

```bash
export CLEANROOM_DATABASE_URL='postgresql+psycopg://cleanroom:cleanroom@localhost:5432/cleanroom'
```

The server defaults to that local PostgreSQL URL when the variable is omitted. SQLite is supported for automated tests and lightweight local development only.

The schema is initialized deterministically at service startup with SQLAlchemy metadata. This is intentionally the first schema version; versioned migrations can be introduced when a later issue changes an existing schema rather than only initializing it.

## Run the API

Install the package and start the persistent server:

```bash
python -m pip install -e .
cleanroom-server
```

Environment overrides:

```bash
export CLEANROOM_HOST=0.0.0.0
export CLEANROOM_PORT=8000
```

OpenAPI documentation is available at `http://localhost:8000/docs` and persistence health at `GET /healthz`.

## Instance boundary

All operational records belong to a first-class cleanroom/environment instance. This boundary exists before full multi-instance product work in #21 so future isolation does not require changing record identity.

Create an instance:

```bash
curl -X POST http://localhost:8000/v1/instances \
  -H 'content-type: application/json' \
  -d '{"id":"lab-a","name":"Lab A","attributes":{"iso_class":"ISO-7"}}'
```

Create a typed task record:

```bash
curl -X POST http://localhost:8000/v1/instances/lab-a/records \
  -H 'content-type: application/json' \
  -d '{"id":"task-1","kind":"task","payload":{"title":"Collect surface sample","status":"queued"}}'
```

Query persisted state:

```bash
curl http://localhost:8000/v1/instances/lab-a/records
curl 'http://localhost:8000/v1/instances/lab-a/records?kind=task'
```

## Persisted record types

The initial typed record boundary supports:

- agents / robots
- tasks
- actions
- risk decisions
- versioned SOP references
- incidents
- operator interventions
- maintenance state/events
- calibration state/events

Issue #23 expands the minimal action/risk records into full provenance and cleanroom policy enforcement. Issue #21 later uses the `instance_id` boundary for strict multi-instance configuration and isolation.

## Tests

The runtime tests use file-backed SQLite so persistence can be restarted within CI without requiring an external PostgreSQL service. They verify:

- durable state across engine/repository restart
- instance-scoped record visibility
- rejection of records for nonexistent instances
- FastAPI create/query behavior

Run the full existing suite plus runtime tests with:

```bash
python -m unittest discover -s tests -v
```
