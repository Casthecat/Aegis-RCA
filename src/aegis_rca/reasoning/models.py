"""Runtime settings and untrusted, provisional model proposals."""

from decimal import Decimal
from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, SecretStr, field_validator, model_validator

from ..models.incident import Contract, Signal


class ReasoningError(Exception):
    """Safe to display: never contains provider bodies, prompts, or credentials."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Reasoning request stopped: {code}")


class BudgetExceeded(ReasoningError):
    pass


class ReasoningConfig(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    endpoint: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr = Field(repr=False, exclude=True)
    request_timeout_seconds: float = Field(default=30, gt=0, le=120, allow_inf_nan=False)
    max_response_bytes: int = Field(default=65536, ge=256, le=1048576)
    allow_http_loopback: bool = False

    @field_validator("model")
    @classmethod
    def clean_model(cls, value):
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("model must be a nonempty identifier without control characters")
        return value

    @field_validator("api_key")
    @classmethod
    def clean_key(cls, value):
        key = value.get_secret_value()
        if not key or key != key.strip() or any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise ValueError("API key must be a nonempty printable ASCII token")
        return value

    @model_validator(mode="after")
    def safe_endpoint(self):
        try:
            parts = urlsplit(self.endpoint)
            host = parts.hostname
            _ = parts.port
        except ValueError:
            raise ValueError("invalid endpoint URL") from None
        if (not host or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or "\\" in self.endpoint
                or any(char.isspace() for char in self.endpoint)
                or not parts.path.endswith("/chat/completions")):
            raise ValueError("endpoint must be a complete chat/completions URL without credentials or query")
        if parts.scheme == "https":
            return self
        try:
            loopback = ip_address(host).is_loopback
        except ValueError:
            loopback = host.lower() == "localhost"
        if parts.scheme != "http" or not self.allow_http_loopback or not loopback:
            raise ValueError("HTTPS is required; explicit loopback HTTP is only for a local provider")
        return self


class ReasoningLimits(Contract):
    # Rates are supplied by the caller. There is no hard-coded model/price table.
    input_usd_per_million: Decimal = Field(ge=0, allow_inf_nan=False)
    output_usd_per_million: Decimal = Field(ge=0, allow_inf_nan=False)
    max_calls: int = Field(default=8, ge=0, le=100)
    max_input_tokens: int = Field(default=24000, ge=0)
    max_output_tokens: int = Field(default=5000, ge=0)
    max_output_tokens_per_call: int = Field(default=1200, ge=1, le=16000)
    max_estimated_cost_usd: Decimal = Field(default=Decimal("0.25"), ge=0, allow_inf_nan=False)
    total_timeout_seconds: float = Field(default=120, gt=0, le=3600, allow_inf_nan=False)


class TaskProposal(Contract):
    signal: Signal
    services: tuple[str, ...] = Field(min_length=1, max_length=10)
    goal: str = Field(min_length=1, max_length=400)


class PlanProposal(Contract):
    incident_key: str = Field(min_length=16, max_length=16)
    tasks: tuple[TaskProposal, ...] = Field(max_length=6)
    rationale: str = Field(min_length=1, max_length=800)
    status: Literal["provisional"]


class HypothesisProposal(Contract):
    id: str = Field(min_length=1, max_length=100)
    claim: str = Field(min_length=1, max_length=800)
    affected_service: str = Field(min_length=1, max_length=200)
    failure_mode: str = Field(min_length=1, max_length=200)
    supporting_evidence_ids: tuple[str, ...] = Field(max_length=20)
    contradicting_evidence_ids: tuple[str, ...] = Field(max_length=20)
    missing_checks: tuple[str, ...] = Field(min_length=1, max_length=6)
    status: Literal["provisional"]


class HypothesisProposalSet(Contract):
    incident_key: str = Field(min_length=16, max_length=16)
    hypotheses: tuple[HypothesisProposal, ...] = Field(max_length=3)
    status: Literal["provisional"]
