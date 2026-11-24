"""The HTTP demo stays fixture-only and bounds its in-process work."""
import asyncio
import json
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
import pytest

import aegis_rca.api as api
from aegis_rca.fixtures import demo_incident


def request(scenario="connectivity", **kwargs):
    return {"incident": demo_incident().model_dump(mode="json"), "scenario": scenario, **kwargs}


async def terminal(client, id):
    for _ in range(200):
        response = await client.get(f"/incidents/{id}")
        status = response.json()
        if status["status"] not in ("queued", "running"):
            return status
        await asyncio.sleep(0.01)
    pytest.fail("demo investigation did not terminate")


@pytest.mark.parametrize("scenario,expected", [("connectivity", "SUPPORTED"), ("missing-logs", "INCONCLUSIVE"), ("empty", "INCONCLUSIVE")])
async def test_fixture_api_completes_and_persists_artifacts(tmp_path, scenario, expected):
    app = api.create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/health")).json()["mode"] == "synthetic_fixture"
            submitted = await client.post("/incidents", json=request(scenario))
            assert submitted.status_code == 202
            assert submitted.json()["status"] == "queued"
            id = submitted.json()["id"]
            assert (await terminal(client, id))["status"] == "completed"
            result = await client.get(f"/incidents/{id}/report")
            assert result.status_code == 200
            assert result.json()["status"] == expected
            assert result.json()["mode"] == "synthetic_fixture"
    assert {path.name for path in (tmp_path / id).iterdir()} == {
        "request.json", "status.json", "report.json", "evidence.json", "trajectory.json", "tool_calls.json",
        "execution.json", "task_specs.json", "attempts.json", "connectivity_verification.json", "investigation_scope.json",
        "snapshot.v2.json", "intent_ledger.json", "scheduling_limitations.json",
        "hypothesis_portfolio.json", "hypothesis_verifications.json"}
    from aegis_rca.tools.snapshot_v2 import read_snapshot_v2
    archive = read_snapshot_v2(tmp_path / id / "snapshot.v2.json")
    assert archive.historical_execution_id == id
    execution = json.loads((tmp_path / id / "execution.json").read_text())
    attempts = json.loads((tmp_path / id / "attempts.json").read_text())
    from aegis_rca.models.scheduling import IntentRecord
    ledger = [IntentRecord.model_validate({k: v for k, v in row.items() if k != "id"})
              for row in json.loads((tmp_path / id / "intent_ledger.json").read_text())]
    assert {r.committed_attempt_id for r in ledger} == {a["id"] for a in attempts}
    assert all(r.status == "completed" for r in ledger)
    assert json.loads((tmp_path / id / "scheduling_limitations.json").read_text()) == []
    from aegis_rca.verification.persisted import PersistedConnectivityVerification
    payload = json.loads((tmp_path / id / "connectivity_verification.json").read_text())
    from aegis_rca.models.portfolio import HypothesisPortfolio
    from aegis_rca.verification.persisted import PersistedHypothesisVerification
    portfolio = HypothesisPortfolio.model_validate_json((tmp_path / id / "hypothesis_portfolio.json").read_text())
    bundles = tuple(PersistedHypothesisVerification.model_validate(row) for row in
        json.loads((tmp_path / id / "hypothesis_verifications.json").read_text()))
    assert tuple(b.hypothesis_spec.id for b in bundles) == tuple(h.id for h in portfolio.active)
    assert payload == (bundles[0].model_dump(mode="json") if len(bundles) == 1 else None)
    if scenario == "empty":
        assert payload is None  # No native diagnostic candidate.
    else:
        bundle = PersistedConnectivityVerification.model_validate(payload)
        assert bundle.evaluation.plan_result.status == ("SUPPORTED" if expected == "SUPPORTED" else "UNCERTAIN")
        assert {r.coverage.acquisition_id for r in bundle.evaluation_basis.resolutions} <= {a["id"] for a in attempts}
    from aegis_rca.models.scope import InvestigationScope
    scope = InvestigationScope.model_validate_json((tmp_path / id / "investigation_scope.json").read_text())
    assert scope.seed_services == ("checkout",)
    assert {e.canonical_witness_attempt_id for e in scope.expansions} <= {a["id"] for a in attempts}
    calls = json.loads((tmp_path / id / "tool_calls.json").read_text())
    assert execution["execution_id"] == id
    assert {item["execution_id"] for item in attempts} == {id}
    assert {item["id"] for item in attempts} == {item["attempt_id"] for item in calls}
    assert json.loads((tmp_path / id / "request.json").read_text(encoding="utf-8"))["scenario"] == scenario


async def test_bounded_concurrency_queue_overflow_and_pending_report(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    real_runner = api.run_investigation
    attempts = []
    active, peak = 0, 0

    async def gated(state, dependencies, **kwargs):
        nonlocal active, peak
        assert not dependencies.attempts
        attempts.append(dependencies.attempts)
        active += 1
        peak = max(peak, active)
        entered.set()
        try:
            await release.wait()
            return await real_runner(state, dependencies, **kwargs)
        finally:
            active -= 1

    monkeypatch.setattr(api, "run_investigation", gated)
    app = api.create_app(tmp_path, max_concurrent=1, max_pending=1)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = (await client.post("/incidents", json=request())).json()["id"]
            await asyncio.wait_for(entered.wait(), 1)
            second = (await client.post("/incidents", json=request())).json()["id"]
            assert (await client.get(f"/incidents/{second}/report")).status_code == 409
            overflow = await client.post("/incidents", json=request())
            assert overflow.status_code == 429
            assert overflow.headers["retry-after"] == "1"
            release.set()
            assert (await terminal(client, first))["status"] == "completed"
            assert (await terminal(client, second))["status"] == "completed"
    assert peak == 1
    assert len(attempts) == 2 and attempts[0] is not attempts[1]
    assert len(list(tmp_path.iterdir())) == 2


async def test_shutdown_cancels_work_and_persists_interrupted(tmp_path, monkeypatch):
    entered = asyncio.Event()

    async def forever(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(api, "run_investigation", forever)
    app = api.create_app(tmp_path, max_concurrent=1, max_pending=1)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = (await client.post("/incidents", json=request())).json()["id"]
            await asyncio.wait_for(entered.wait(), 1)
            second = (await client.post("/incidents", json=request())).json()["id"]
    for id in (first, second):
        saved = json.loads((tmp_path / id / "status.json").read_text(encoding="utf-8"))
        assert saved["status"] == "interrupted"
        assert saved["error"] == "service_shutdown"
        assert not (tmp_path / id / "report.json").exists()


async def test_restart_loads_completed_report_and_interrupts_stale_jobs(tmp_path):
    app = api.create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            id = (await client.post("/incidents", json=request())).json()["id"]
            await terminal(client, id)
            before = (await client.get(f"/incidents/{id}/report")).json()
    stale_id = str(uuid4())
    (tmp_path / stale_id).mkdir()
    (tmp_path / stale_id / "status.json").write_text(api.JobStatus(id=stale_id, status="running", updated_at="recorded").model_dump_json(), encoding="utf-8")
    restarted = api.create_app(tmp_path)
    async with restarted.router.lifespan_context(restarted):
        async with AsyncClient(transport=ASGITransport(app=restarted), base_url="http://test") as client:
            assert (await client.get(f"/incidents/{id}/report")).json() == before
            status = (await client.get(f"/incidents/{stale_id}")).json()
            assert status["status"] == "interrupted"
            assert status["error"] == "interrupted_after_restart"
            assert (await client.get(f"/incidents/{stale_id}/report")).status_code == 409


async def test_failed_jobs_sanitize_exception_details(tmp_path, monkeypatch):
    async def failing(*args, **kwargs):
        raise RuntimeError("secret-token C:/private/config.env")
    monkeypatch.setattr(api, "run_investigation", failing)
    app = api.create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            id = (await client.post("/incidents", json=request())).json()["id"]
            status = await terminal(client, id)
            assert status["status"] == "failed"
            assert status["error"] == "investigation_failed"
            assert "secret-token" not in json.dumps(status)
            assert (await client.get(f"/incidents/{id}/report")).status_code == 409


async def test_global_investigation_deadline_still_produces_conservative_report(tmp_path, monkeypatch):
    async def slow_query(*args, **kwargs):
        await asyncio.sleep(1)
    monkeypatch.setattr(api.FixtureBackend, "query", slow_query)
    app = api.create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            id = (await client.post("/incidents", json=request(deadline_seconds=0.01))).json()["id"]
            assert (await terminal(client, id))["status"] == "completed"
            report = (await client.get(f"/incidents/{id}/report")).json()
            assert report["status"] == "INCONCLUSIVE"
            assert report["termination_reason"] == "deadline_exhausted"


async def test_unknown_ids_and_path_traversal_never_read_files(tmp_path):
    app = api.create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for suffix in (str(uuid4()), "not-a-uuid", "%2e%2e%2fsecret", "C:%5Cprivate"):
                assert (await client.get(f"/incidents/{suffix}")).status_code == 404
                assert (await client.get(f"/incidents/{suffix}/report")).status_code == 404


@pytest.mark.parametrize("extra", [{"snapshot": "C:/private/file.json"}, {"endpoint": "https://example.com"}, {"api_key": "secret"}, {"scenario": "live"}, {"budget": {"max_tool_calls": 1000}}, {"deadline_seconds": 121}])
async def test_public_request_rejects_live_configuration_and_unbounded_budget(tmp_path, extra):
    app = api.create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            payload = request()
            payload.update(extra)
            assert (await client.post("/incidents", json=payload)).status_code == 422
    assert list(tmp_path.iterdir()) == []


async def test_restart_does_not_claim_complete_when_report_missing(tmp_path):
    id = str(uuid4())
    (tmp_path / id).mkdir()
    (tmp_path / id / "status.json").write_text(api.JobStatus(id=id, status="completed", updated_at="recorded").model_dump_json(), encoding="utf-8")
    app = api.create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            status = (await client.get(f"/incidents/{id}")).json()
            assert status["status"] == "failed"
            assert status["error"] == "artifact_unavailable"
