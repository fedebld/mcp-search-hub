"""Token bucket + circuit breaker rate limiter."""
import asyncio
import time
import logging
from enum import Enum

logger = logging.getLogger("rate_limiter")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    pass


class RateLimitError(Exception):
    pass


class BackendHealthError(Exception):
    pass


class RateLimiter:
    def __init__(
        self,
        name: str,
        rate_per_minute: int,
        burst: int,
        cool_down_seconds: int,
        failure_threshold: int = 3,
    ):
        self.name = name
        self.rate_per_minute = rate_per_minute
        self.burst = burst
        self.cooldown = cool_down_seconds
        self.failure_threshold = failure_threshold

        self._lock = asyncio.Lock()
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> CircuitState:
        return self._state

    async def acquire(self) -> bool:
        async with self._lock:
            now = time.monotonic()

            if self._state == CircuitState.OPEN:
                if self._opened_at and (now - self._opened_at) >= self.cooldown:
                    self._state = CircuitState.HALF_OPEN
                    logger.info("[%s] HALF_OPEN -- probe allowed", self.name)
                else:
                    remaining = 0
                    if self._opened_at:
                        remaining = max(0, int(self.cooldown - (now - self._opened_at)))
                    raise CircuitOpenError(f"{self.name} OPEN for {remaining}s")

            elapsed = now - self._last_refill
            refill = elapsed * (self.rate_per_minute / 60.0)
            self._tokens = min(self.burst, self._tokens + refill)
            self._last_refill = now

            if self._tokens < 1.0:
                wait_time = (1.0 - self._tokens) / (self.rate_per_minute / 60.0)
                raise RateLimitError(f"{self.name} rate limited -- retry in {wait_time:.1f}s")

            self._tokens -= 1.0
            return True

    async def record_success(self):
        async with self._lock:
            self._failures = 0
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                logger.info("[%s] Circuit CLOSED -- recovered", self.name)

    async def record_failure(self):
        async with self._lock:
            self._failures += 1
            logger.warning("[%s] Failure %d/%d", self.name, self._failures, self.failure_threshold)
            if self._failures >= self.failure_threshold:
                if self._state == CircuitState.HALF_OPEN:
                    self._state = CircuitState.OPEN
                    self._opened_at = time.monotonic()
                    logger.error("[%s] Re-OPENED after failed probe", self.name)
                elif self._state == CircuitState.CLOSED:
                    self._state = CircuitState.OPEN
                    self._opened_at = time.monotonic()
                    logger.error("[%s] Circuit OPENED (cooldown: %ds)", self.name, self.cooldown)
