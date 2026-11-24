"""Frozen Demo resource environment and offline prepared-environment validation.

These contracts describe collection infrastructure, not an investigation treatment.
Preparing/validating them neither starts containers nor changes fault controls.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from ..models.identity import structural_id
from ..models.incident import Contract

DEMO_VERSION = "3.0.0"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
SOURCE_REPOSITORY = "https://github.com/open-telemetry/opentelemetry-demo"
SOURCE_ARCHIVE_URL = "https://codeload.github.com/open-telemetry/opentelemetry-demo/zip/refs/tags/3.0.0"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DemoServiceResourceOverride(Contract):
    service: str = Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    memory_limit_bytes: StrictInt = Field(gt=0, le=1024 ** 4)
    gomemlimit_bytes: StrictInt = Field(gt=0, le=1024 ** 4)

    @model_validator(mode="after")
    def go_limit_has_headroom(self):
        if self.gomemlimit_bytes >= self.memory_limit_bytes:
            raise ValueError("GOMEMLIMIT must leave headroom below the container memory limit")
        return self


class DemoResourceProfile(Contract):
    schema_version: Literal[1] = 1
    profile_name: str = Field(min_length=1, max_length=160)
    otel_demo_version: Literal["3.0.0"] = DEMO_VERSION
    service_overrides: tuple[DemoServiceResourceOverride, ...] = Field(min_length=1, max_length=64)

    @field_validator("profile_name")
    @classmethod
    def named_profile(cls, value):
        if value != value.strip() or not value:
            raise ValueError("resource profile name must be nonblank and canonical")
        return value

    @field_validator("service_overrides")
    @classmethod
    def canonical_overrides(cls, value):
        names = [item.service for item in value]
        if len(set(names)) != len(names):
            raise ValueError("duplicate service resource override")
        return tuple(sorted(value, key=lambda item: item.service))

    @property
    def id(self):
        return structural_id("DRP-", self.model_dump())


# Historical frozen data: keep the checkout-only revision replayable and
# preserve its structural identity for existing pilot-02 policy artifacts.
DEMO_RESOURCE_PROFILE_V1 = DemoResourceProfile(
    profile_name="pilot-checkout-headroom-v1",
    service_overrides=(DemoServiceResourceOverride(
        service="checkout", memory_limit_bytes=128 * 1024 ** 2,
        gomemlimit_bytes=96 * 1024 ** 2),),
)


DEMO_RESOURCE_PROFILE_V2 = DemoResourceProfile(
    profile_name="pilot-checkout-product-catalog-headroom-v2",
    service_overrides=(
        DemoServiceResourceOverride(service="checkout", memory_limit_bytes=128 * 1024 ** 2,
                                    gomemlimit_bytes=96 * 1024 ** 2),
        DemoServiceResourceOverride(service="product-catalog", memory_limit_bytes=128 * 1024 ** 2,
                                    gomemlimit_bytes=96 * 1024 ** 2),
    ),
)

PILOT_DEMO_RESOURCE_PROFILE = DEMO_RESOURCE_PROFILE_V2


def apply_resource_profile(config: dict, profile: DemoResourceProfile) -> dict:
    """Change only explicitly selected resource fields of resolved Compose data."""
    result = deepcopy(config)
    for override in profile.service_overrides:
        try:
            service = result["services"][override.service]
        except KeyError as exc:
            raise ValueError("resource profile names a missing Compose service") from exc
        environment = service.setdefault("environment", {})
        if not isinstance(environment, dict):
            raise ValueError("resource profiles require resolved Compose environment mappings")
        service.setdefault("deploy", {}).setdefault("resources", {}).setdefault("limits", {})[
            "memory"] = str(override.memory_limit_bytes)
        environment["GOMEMLIMIT"] = f"{override.gomemlimit_bytes}B"
    return result


def _validate_overrides(config: dict, profile: DemoResourceProfile):
    """A receipt cannot claim overrides absent from its bound generated Compose."""
    for override in profile.service_overrides:
        try:
            service = config["services"][override.service]
            memory = service["deploy"]["resources"]["limits"]["memory"]
            gomemlimit = service["environment"]["GOMEMLIMIT"]
        except (KeyError, TypeError) as exc:
            raise ValueError("generated Compose is missing resource profile fields") from exc
        if memory != str(override.memory_limit_bytes) or gomemlimit != f"{override.gomemlimit_bytes}B":
            raise ValueError("generated Compose resources differ from the declared profile")


class DemoUpstreamSourceMarker(Contract):
    repository: Literal[SOURCE_REPOSITORY]
    tag: Literal["3.0.0"]
    archive_url: Literal[SOURCE_ARCHIVE_URL]


class PreparedDemoEnvironment(Contract):
    schema_version: Literal[1] = 1
    otel_demo_version: Literal["3.0.0"] = DEMO_VERSION
    demo_resource_profile: DemoResourceProfile
    demo_resource_profile_id: str = Field(pattern=r"^DRP-[0-9a-f]{64}$")
    upstream_source_marker: DemoUpstreamSourceMarker
    upstream_source_marker_sha256: str = Field(pattern=SHA256_PATTERN)
    upstream_compose_sha256: str = Field(pattern=SHA256_PATTERN)
    upstream_observability_compose_sha256: str = Field(pattern=SHA256_PATTERN)
    resolved_compose_sha256: str = Field(pattern=SHA256_PATTERN)
    generated_compose_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def identities_match(self):
        if self.demo_resource_profile_id != self.demo_resource_profile.id:
            raise ValueError("prepared environment resource profile identity mismatch")
        if self.otel_demo_version != self.demo_resource_profile.otel_demo_version:
            raise ValueError("prepared environment Demo version mismatch")
        return self

    @property
    def id(self):
        return structural_id("DEP-", self.model_dump())


def build_prepared_environment(profile: DemoResourceProfile, compose_bytes: bytes,
        source_marker_bytes: bytes, upstream_compose_bytes: bytes,
        upstream_observability_bytes: bytes, resolved_compose_bytes: bytes) -> PreparedDemoEnvironment:
    marker = DemoUpstreamSourceMarker.model_validate_json(source_marker_bytes)
    config = json.loads(compose_bytes)
    _validate_overrides(config, profile)
    return PreparedDemoEnvironment(
        demo_resource_profile=profile, demo_resource_profile_id=profile.id,
        upstream_source_marker=marker,
        upstream_source_marker_sha256=sha256(source_marker_bytes),
        upstream_compose_sha256=sha256(upstream_compose_bytes),
        upstream_observability_compose_sha256=sha256(upstream_observability_bytes),
        resolved_compose_sha256=sha256(resolved_compose_bytes),
        generated_compose_sha256=sha256(compose_bytes),
    )


def validate_prepared_environment(compose_path: Path, receipt_path: Path, *,
        expected_profile_id: str, expected_demo_version: str) -> PreparedDemoEnvironment:
    """Verify recorded preparation and current generated bytes without Docker/IO beyond files.

    This binds the prepared configuration. Operator health/readiness and use of this
    Compose project remain distinct checks; this is not a container-runtime attestation.
    """
    receipt = PreparedDemoEnvironment.model_validate_json(Path(receipt_path).read_bytes())
    if receipt.demo_resource_profile_id != expected_profile_id:
        raise ValueError("prepared environment resource profile differs from CapturePolicy")
    if receipt.otel_demo_version != expected_demo_version:
        raise ValueError("prepared environment Demo version differs from CapturePolicy")
    compose_bytes = Path(compose_path).read_bytes()
    if sha256(compose_bytes) != receipt.generated_compose_sha256:
        raise ValueError("generated Compose bytes differ from the prepared environment receipt")
    _validate_overrides(json.loads(compose_bytes), receipt.demo_resource_profile)
    return receipt
