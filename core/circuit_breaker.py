"""
Circuit breaker for the Groq LLM call in classifier.py.

If the LLM fails N times in a row, stop calling it for a cooldown window and
fall back to rule-based-only mode -- and log that we did so. This protects
the pipeline's latency and reliability from a flaky or rate-limited external
API; the rule-based classifier keeps working regardless of breaker state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    cooldown_seconds: float = 60.0
    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    trip_count: int = field(default=0, init=False)  # how many times it has opened, ever

    def allow_call(self) -> bool:
        if self._opened_at is None:
            return True
        elapsed = time.time() - self._opened_at
        if elapsed >= self.cooldown_seconds:
            # cooldown elapsed -- half-open: allow one probe call through.
            return True
        return False

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            if self._opened_at is None:
                self.trip_count += 1
            self._opened_at = time.time()  # (re)start cooldown -- also covers a failed half-open probe

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None and not self.allow_call()

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        return "open" if not self.allow_call() else "half_open"
