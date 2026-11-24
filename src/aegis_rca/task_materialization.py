"""Resolve physical query defaults at runtime, without involving planner policy."""

from .models.acquisition import LogQueryScope, TaskSpec
from .models.incident import InvestigationTask


class UnsupportedAcquisition(ValueError):
    """No physical query contract exists; no T/A or budget charge is created."""


class UnauthorizedAcquisition(PermissionError):
    pass


def authorize_task(task, state):
    from .investigation_scope import current_investigation_scope
    scope = current_investigation_scope(state)
    allowed = scope.admitted_services if scope is not None else state["incident"].seed_services
    window = state["incident"].window
    if (not task.services or not set(task.services) <= set(allowed)
            or task.window.start < window.start or task.window.end > window.end):
        raise UnauthorizedAcquisition("task exceeds current investigation permissions")


def check_capability(task, backend):
    from .tools.recorded_replay import RecordedReplayBackend
    from .fixtures import FixtureBackend
    from .tools.live import LiveBackend
    from .tools.replay import ReplayBackend
    services = tuple(sorted(set(task.services)))
    if isinstance(backend, RecordedReplayBackend):
        backend.select(task)
    elif isinstance(backend, LiveBackend):
        if len(services) != 1:
            raise UnsupportedAcquisition("Live physical acquisitions require exactly one service")
        if task.signal == "metric" and set(services) != set(backend.incident.seed_services):
            raise UnsupportedAcquisition("fixed PromQL cannot acquire an expanded service")
        if task.signal not in {"trace", "log", "metric", "knowledge"}:
            raise UnsupportedAcquisition("unsupported Live signal")
        if task.window.start < backend.incident.window.start or task.window.end > backend.incident.window.end:
            raise UnsupportedAcquisition("Live request exceeds its configured window")
    elif isinstance(backend, ReplayBackend):
        if (not set(services) <= set(backend.snapshot.incident.seed_services)
                or task.window.start < backend.snapshot.incident.window.start
                or task.window.end > backend.snapshot.incident.window.end):
            raise UnsupportedAcquisition("Replay V1 supports only recorded seed scope")
    elif isinstance(backend, FixtureBackend):
        if not set(services) <= set(backend.seed_services):
            if task.signal not in {"trace", "log"} or (task.signal, services) not in backend.materials:
                raise UnsupportedAcquisition("expanded fixture acquisition requires explicitly injected trace/log material")


def materialize_task(task: InvestigationTask, backend) -> TaskSpec:
    check_capability(task, backend)
    from .tools.recorded_replay import RecordedReplayBackend
    if isinstance(backend, RecordedReplayBackend):
        return backend.materialize(task)
    # Imports are local to avoid coupling backend/runtime module initialization.
    from .fixtures import FixtureBackend
    from .tools.live import LiveBackend
    from .tools.replay import ReplayBackend

    values = dict(signal=task.signal, services=task.services, window=task.window)
    if isinstance(backend, LiveBackend):
        settings = backend.settings
        if task.signal == "metric" and settings.metric_query is not None:
            return TaskSpec(**values, query_template_id="prometheus.query_range.v1",
                            query_expression=settings.metric_query, result_limit=settings.max_results,
                            metric_step_seconds=settings.metric_step_seconds)
        if task.signal == "trace":
            return TaskSpec(**values, query_template_id="jaeger.search.v3", result_limit=settings.max_results)
        if task.signal == "log":
            adapter = backend.opensearch
            return TaskSpec(**values, query_template_id="opensearch.search.v1", result_limit=settings.max_results,
                log_query=LogQueryScope(index=adapter.index, timestamp_field=adapter.fields.timestamp,
                    service_exact_field=adapter.fields.service_exact,
                    server_timeout_ms=int(adapter.http.timeout_seconds * 1000)))
        return TaskSpec(**values, query_template_id=f"unconfigured.{task.signal}.v1")
    template = ("fixture.select.v1" if isinstance(backend, FixtureBackend) else
                "replay.select.v1" if isinstance(backend, ReplayBackend) else "tool.query.v1")
    return TaskSpec(**values, query_template_id=template)
