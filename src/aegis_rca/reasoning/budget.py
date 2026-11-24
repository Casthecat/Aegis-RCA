"""Incident-scoped reservations; failed and cancelled calls receive no refund."""

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from .models import BudgetExceeded, ReasoningLimits


@dataclass(frozen=True)
class BudgetSnapshot:
    calls: int
    reserved_input_tokens: int
    reserved_output_tokens: int
    estimated_cost_usd: Decimal


@dataclass(frozen=True)
class Reservation:
    call_number: int
    input_tokens: int
    max_output_tokens: int
    estimated_cost_usd: Decimal


def conservative_input_tokens(serialized_request: bytes) -> int:
    """UTF-8 request bytes + 1024 framing margin, including the output schema.

    Deliberately overestimates ordinary byte-tokenized text requests. This is a
    local reservation estimate, not a guarantee for arbitrary provider tokenizers.
    """
    return len(serialized_request) + 1024


class BudgetManager:
    def __init__(self, limits: ReasoningLimits, *, clock: Callable[[], float] = time.monotonic):
        self.limits = limits
        self._clock = clock
        self._deadline = clock() + limits.total_timeout_seconds
        self._lock = asyncio.Lock()
        self._snapshot = BudgetSnapshot(0, 0, 0, Decimal(0))

    @property
    def snapshot(self) -> BudgetSnapshot:
        return self._snapshot

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self._deadline - self._clock())

    async def reserve(self, input_tokens: int, max_output_tokens: int | None = None) -> Reservation:
        output_tokens = (self.limits.max_output_tokens_per_call
                         if max_output_tokens is None else max_output_tokens)
        if (isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0
                or isinstance(output_tokens, bool) or not isinstance(output_tokens, int)
                or not 1 <= output_tokens <= self.limits.max_output_tokens_per_call):
            raise ValueError("invalid reservation token counts")
        async with self._lock:
            current = self._snapshot
            if self.remaining_seconds <= 0:
                raise BudgetExceeded("deadline_exhausted")
            cost = (Decimal(input_tokens) * self.limits.input_usd_per_million
                    + Decimal(output_tokens) * self.limits.output_usd_per_million) / Decimal(1000000)
            if current.calls + 1 > self.limits.max_calls:
                raise BudgetExceeded("call_budget_exhausted")
            if current.reserved_input_tokens + input_tokens > self.limits.max_input_tokens:
                raise BudgetExceeded("input_token_budget_exhausted")
            if current.reserved_output_tokens + output_tokens > self.limits.max_output_tokens:
                raise BudgetExceeded("output_token_budget_exhausted")
            if current.estimated_cost_usd + cost > self.limits.max_estimated_cost_usd:
                raise BudgetExceeded("estimated_cost_budget_exhausted")
            self._snapshot = BudgetSnapshot(
                current.calls + 1, current.reserved_input_tokens + input_tokens,
                current.reserved_output_tokens + output_tokens, current.estimated_cost_usd + cost,
            )
            return Reservation(current.calls + 1, input_tokens, output_tokens, cost)
