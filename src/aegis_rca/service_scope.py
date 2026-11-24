"""Bounded logical triage, independent of physical backend capabilities."""
from .models.incident import IncidentInput
from .models.scope import ScopeExpansionPolicy

MAX_SEED_SERVICES = 64  # Same upper service-count bound as the scope policy contract.
MAX_SERVICE_NAME_LENGTH = ScopeExpansionPolicy().max_service_name_length


def valid_service(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_SERVICE_NAME_LENGTH


def canonical_incident(incident):
    incident = IncidentInput.model_validate(incident.model_dump())
    if len(incident.seed_services) > MAX_SEED_SERVICES or not all(map(valid_service, incident.seed_services)):
        raise ValueError("incident seeds exceed the bounded service identifier contract")
    return incident.model_copy(update={"seed_services": tuple(sorted(set(incident.seed_services)))})
