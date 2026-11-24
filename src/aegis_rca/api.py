"""Local synthetic-fixture demo: uvicorn aegis_rca.api:app --host 127.0.0.1.

Single-process job persistence is not graph checkpoint/resume or production auth.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException
from pydantic import Field

from .fixtures import FixtureBackend, demo_evidence, demo_traces
from .models.hypothesis import IncidentReport
from .models.incident import Budget, Contract, IncidentInput
from .runner import run_investigation
from .runtime import Dependencies
from .state import initial_state


class SubmitIncident(Contract):
    incident: IncidentInput
    scenario: Literal["connectivity", "missing-logs", "empty"] = "connectivity"
    budget: Budget = Field(default_factory=Budget)
    deadline_seconds: float = Field(default=120, gt=0, le=120, allow_inf_nan=False)


class JobStatus(Contract):
    id: str
    status: Literal["queued", "running", "completed", "failed", "interrupted"]
    mode: Literal["synthetic_fixture"] = "synthetic_fixture"
    updated_at: str
    error: Literal["investigation_failed", "interrupted_after_restart", "service_shutdown", "artifact_unavailable"] | None = None


def create_app(output_dir: Path, max_concurrent: int = 2, max_pending: int = 10) -> FastAPI:
    if isinstance(max_concurrent, bool) or not isinstance(max_concurrent, int) or not 1 <= max_concurrent <= 32:
        raise ValueError("max_concurrent must be an integer in 1..32")
    if isinstance(max_pending, bool) or not isinstance(max_pending, int) or not 0 <= max_pending <= 1000:
        raise ValueError("max_pending must be an integer in 0..1000")
    root = Path(output_dir).resolve()
    statuses: dict[str, JobStatus] = {}
    jobs: dict[str, asyncio.Task] = {}
    semaphore = asyncio.Semaphore(max_concurrent)
    accepting = False

    def write(path: Path, payload):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(path)

    def update(id: str, status: str, error=None):
        value = JobStatus(id=id, status=status, error=error, updated_at=datetime.now(timezone.utc).isoformat())
        write(root / id / "status.json", value.model_dump(mode="json"))
        statuses[id] = value
        return value

    def fail_safely(id: str, status: str, error: str):
        try:
            update(id, status, error)
        except OSError:
            statuses[id] = JobStatus(id=id, status=status, error=error, updated_at=datetime.now(timezone.utc).isoformat())

    async def execute(id: str, request: SubmitIncident):
        try:
            async with semaphore:
                update(id, "running")
                records = demo_evidence()
                if request.scenario == "empty":
                    records = ()
                elif request.scenario == "missing-logs":
                    records = tuple(item for item in records if item.signal != "log")
                raw_fixture = FixtureBackend()
                dependencies = Dependencies(backend=FixtureBackend(records,
                    traces=demo_traces() if request.scenario != "empty" else None,
                    logs=raw_fixture.logs if request.scenario == "connectivity" else None,
                    metrics=raw_fixture.metrics if request.scenario != "empty" else None))
                state, events = await run_investigation(
                    initial_state(request.incident, request.budget, source_mode="synthetic_fixture", execution_id=id),
                    dependencies,
                    deadline_seconds=request.deadline_seconds)
                report = state["final_report"]
                if report is None:
                    raise ValueError("Investigation returned no report")
                from .tools.snapshot_v2 import export_snapshot_v2, write_snapshot_v2
                write_snapshot_v2(export_snapshot_v2(state, dependencies), root / id / "snapshot.v2.json")
                write(root / id / "report.json", report.model_dump(mode="json"))
                write(root / id / "evidence.json", [item.model_dump(mode="json") for item in state["evidence"]])
                write(root / id / "trajectory.json", events)
                write(root / id / "tool_calls.json", [item.model_dump(mode="json") for item in state["tool_calls"]])
                write(root / id / "execution.json", {"execution_id": state["execution_id"]})
                write(root / id / "intent_ledger.json", [{"id": r.id, **r.model_dump(mode="json")} for r in state["intent_ledger"]])
                write(root / id / "scheduling_limitations.json", [r.model_dump(mode="json") for r in state["scheduling_limitations"]])
                write(root / id / "task_specs.json", [{"id": item.id, **item.model_dump(mode="json")} for item in state["task_specs"]])
                write(root / id / "attempts.json", [{"id": item.id, **item.model_dump(mode="json")} for item in state["attempt_records"]])
                bundle = state["hypothesis_verifications"][0] if len(state["hypothesis_verifications"]) == 1 else None
                write(root / id / "connectivity_verification.json", bundle.model_dump(mode="json") if bundle else None)
                portfolio = state["hypothesis_portfolio"]
                write(root / id / "hypothesis_portfolio.json", portfolio.model_dump(mode="json") if portfolio else None)
                write(root / id / "hypothesis_verifications.json", [b.model_dump(mode="json") for b in state["hypothesis_verifications"]])
                scope = state["investigation_scope"]
                write(root / id / "investigation_scope.json", scope.model_dump(mode="json") if scope else None)
                update(id, "completed")
        except asyncio.CancelledError:
            fail_safely(id, "interrupted", "service_shutdown")
            raise
        except Exception:
            fail_safely(id, "failed", "investigation_failed")
        finally:
            jobs.pop(id, None)

    @asynccontextmanager
    async def lifespan(app):
        nonlocal accepting, semaphore
        semaphore = asyncio.Semaphore(max_concurrent)
        statuses.clear()
        root.mkdir(parents=True, exist_ok=True)
        for directory in root.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            try:
                if str(UUID(directory.name)) != directory.name:
                    continue
                stored = JobStatus.model_validate_json((directory / "status.json").read_text(encoding="utf-8"))
                if stored.id != directory.name:
                    continue
                statuses[stored.id] = stored
                if stored.status in ("queued", "running"):
                    update(stored.id, "interrupted", "interrupted_after_restart")
                elif stored.status == "completed":
                    try:
                        IncidentReport.model_validate_json((directory / "report.json").read_text(encoding="utf-8"))
                    except (ValueError, OSError):
                        update(stored.id, "failed", "artifact_unavailable")
            except (ValueError, OSError):
                continue
        accepting = True
        try:
            yield
        finally:
            accepting = False
            active = list(jobs.values())
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
            for id, status in list(statuses.items()):
                if status.status in ("queued", "running"):
                    fail_safely(id, "interrupted", "service_shutdown")
            jobs.clear()

    app = FastAPI(title="AegisRCA local synthetic demo", lifespan=lifespan)

    def lookup(id: str):
        if id not in statuses:
            raise HTTPException(404, "Unknown incident")
        return statuses[id]

    @app.get("/health")
    async def health():
        return {"status": "ok" if accepting else "starting", "mode": "synthetic_fixture",
                "max_concurrent": max_concurrent, "max_pending": max_pending}

    @app.post("/incidents", status_code=202)
    async def submit(request: SubmitIncident):
        if not accepting:
            raise HTTPException(503, "Service is shutting down or not started")
        if len(jobs) >= max_concurrent + max_pending:
            raise HTTPException(429, "Incident queue is full", headers={"Retry-After": "1"})
        id = str(uuid4())
        try:
            (root / id).mkdir()
            write(root / id / "request.json", request.model_dump(mode="json"))
            status = update(id, "queued")
        except OSError:
            raise HTTPException(503, "Incident storage unavailable") from None
        jobs[id] = asyncio.create_task(execute(id, request))
        return status.model_dump(mode="json")

    @app.get("/incidents/{id}")
    async def get_status(id: str):
        return lookup(id).model_dump(mode="json")

    @app.get("/incidents/{id}/report")
    async def get_report(id: str):
        status = lookup(id)
        if status.status != "completed":
            raise HTTPException(409, {"status": status.status, "error": status.error})
        try:
            report = IncidentReport.model_validate_json((root / id / "report.json").read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise HTTPException(503, "Report artifact unavailable") from None
        return report.model_dump(mode="json")

    return app


app = create_app(Path("artifacts/api"))
