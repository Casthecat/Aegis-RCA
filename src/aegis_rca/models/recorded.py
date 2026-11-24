"""Path-independent recorded acquisition selection and transport lineage."""

from pydantic import Field

from .incident import Contract, InvestigationTask


class RecordedAcquisitionSelector(Contract):
    replay_manifest_id: str = Field(pattern=r"^RM-[0-9a-f]{64}$")
    recorded_attempt_id: str = Field(pattern=r"^A-[0-9a-f]{64}$")
    recorded_task_spec_id: str = Field(pattern=r"^T-[0-9a-f]{64}$")


class RecordedSourceReference(RecordedAcquisitionSelector):
    recorded_content_digest: str = Field(pattern=r"^sha256-[0-9a-f]{64}$")


class RecordedTaskRequest(InvestigationTask):
    """Explicit historical A selection; logical scope remains ordinary intent."""

    recorded_selection: RecordedAcquisitionSelector
