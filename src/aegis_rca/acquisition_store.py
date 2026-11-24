"""One orchestration-owned terminal commit per A-*, including its raw partition."""

from threading import Lock

from .models.acquisition_result import CommittedAcquisition


class AlreadyCommitted(ValueError):
    pass


class AcquisitionStore:
    def __init__(self):
        self._terminal: dict[str, str] = {}
        self._lock = Lock()

    def commit(self, completion: CommittedAcquisition):
        # Serialized ownership isolates even mutable nested telemetry
        # dictionaries from backend buffers and from readers' model copies.
        frozen = CommittedAcquisition.model_validate_json(completion.model_dump_json())
        payload = frozen.model_dump_json()
        with self._lock:
            if frozen.attempt.id in self._terminal:
                raise AlreadyCommitted("attempt already has an authoritative terminal commit")
            self._terminal[frozen.attempt.id] = payload
        return frozen

    def get(self, attempt_id):
        with self._lock:
            payload = self._terminal.get(attempt_id)
        return CommittedAcquisition.model_validate_json(payload) if payload is not None else None

    def partition_for(self, attempt_id):
        completion = self.get(attempt_id)
        return completion.partition if completion else None

    def values(self):
        with self._lock:
            payloads = tuple(self._terminal.values())
        return tuple(CommittedAcquisition.model_validate_json(payload) for payload in payloads)

    def ids(self):
        with self._lock:
            return frozenset(self._terminal)


class PartitionReader:
    """Read-only facade supplied to source/export compatibility callers."""

    def __init__(self, store):
        self.__store = store

    def partition_for(self, attempt_id):
        return self.__store.partition_for(attempt_id)

    def values(self):
        return self.__store.values()
