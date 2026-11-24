"""Exact raw acquisition replay with new transport identities and historical lineage."""

from ..models.acquisition import MaterializedTask, TaskSpec
from ..models.acquisition_result import AcquisitionResult
from ..models.recorded import RecordedAcquisitionSelector, RecordedSourceReference
from ..task_materialization import UnsupportedAcquisition
from .projection import project_material
from .snapshot_v2 import SnapshotV2


class RecordedAcquisitionUnavailable(UnsupportedAcquisition):
    pass


class AmbiguousRecordedAcquisition(UnsupportedAcquisition):
    pass


class RecordedReplayBackend:
    def __init__(self, snapshot: SnapshotV2):
        self._archive = SnapshotV2.model_validate_json(snapshot.model_dump_json())
        self._specs = {t.id: t for t in self._archive.task_specs}
        self._rows = {r.attempt.id: r for r in self._archive.recorded_acquisitions}

    @property
    def snapshot(self):
        return self._archive.model_copy(deep=True)

    def validate_execution_namespace(self, execution_id):
        if execution_id == self._archive.historical_execution_id:
            raise ValueError("recorded replay requires a new execution namespace")

    def selector_for_attempt(self, attempt_id):
        row = self._rows.get(attempt_id)
        if row is None or row.partition is None or row.attempt.tool_status not in {"ok", "empty"}:
            raise RecordedAcquisitionUnavailable("historical A has no replayable raw partition")
        return RecordedAcquisitionSelector(replay_manifest_id=self._archive.replay_manifest_id,
            recorded_attempt_id=attempt_id, recorded_task_spec_id=row.attempt.task_spec_id)

    def selector_for_task(self, task_spec_id):
        return self._unique([r for r in self._rows.values() if r.attempt.task_spec_id == task_spec_id
                             and r.partition is not None])

    def _unique(self, rows):
        if not rows:
            raise RecordedAcquisitionUnavailable("no matching recorded raw acquisition")
        if len(rows) != 1:
            raise AmbiguousRecordedAcquisition("multiple historical A candidates; select an explicit A")
        return self.selector_for_attempt(rows[0].attempt.id)

    def select(self, task):
        selector = (task.task_spec.recorded_selection if isinstance(task, MaterializedTask)
                    else getattr(task, "recorded_selection", None))
        def matches(row):
            spec = self._specs[row.attempt.task_spec_id]
            return (spec.signal == task.signal and spec.services == tuple(sorted(set(task.services)))
                    and spec.window == task.window)
        if selector is None:
            if isinstance(task, MaterializedTask):
                raise RecordedAcquisitionUnavailable("recorded transport requires TaskSpec v2 selection")
            # Logical discovery is deliberately conservative for every signal,
            # including metrics: any second A or distinct PromQL is ambiguous.
            selector = self._unique([r for r in self._rows.values() if r.partition is not None and matches(r)])
        selector = RecordedAcquisitionSelector.model_validate(selector.model_dump())
        actual = self.selector_for_attempt(selector.recorded_attempt_id)
        if selector != actual:
            raise RecordedAcquisitionUnavailable("recorded selector manifest/T reference mismatch")
        row = self._rows[selector.recorded_attempt_id]
        if not matches(row):
            raise RecordedAcquisitionUnavailable("recorded replay requires exact signal/services/window")
        return selector

    def materialize(self, task):
        return TaskSpec(schema_version=2, signal=task.signal, services=task.services, window=task.window,
                        query_template_id="replay.select.recorded.v1", recorded_selection=self.select(task))

    async def acquire(self, task):
        selection = self.select(task)
        part = self._rows[selection.recorded_attempt_id].partition.model_copy(deep=True)
        reference = RecordedSourceReference(**selection.model_dump(), recorded_content_digest=part.content_digest)
        return AcquisitionResult(signal=task.signal, projection=project_material(task, part.material, part.source),
                                 material=part.material, source=part.source, recorded_from=reference)

    async def query(self, task):
        return (await self.acquire(task)).projection
