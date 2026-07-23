from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
import threading
import time
from typing import Any, Callable, Dict, Optional


class ExternalServiceError(RuntimeError):
    """Base error raised by the bounded external-service runtime."""


class ExternalServiceTimeoutError(ExternalServiceError):
    pass


class ExternalServiceBusyError(ExternalServiceError):
    pass


class ExternalServiceCircuitOpenError(ExternalServiceError):
    pass


class ExternalServiceCallError(ExternalServiceError):
    pass


@dataclass(frozen=True)
class ExternalServicePolicy:
    timeout_seconds: float
    max_attempts: int
    max_concurrency: int
    failure_threshold: int = 3
    recovery_seconds: float = 30.0
    backoff_seconds: float = 0.2
    acquire_timeout_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        if self.max_attempts < 1:
            raise ValueError("max_attempts 必须大于等于 1")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency 必须大于等于 1")
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold 必须大于等于 1")
        if self.recovery_seconds < 0:
            raise ValueError("recovery_seconds 不能小于 0")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds 不能小于 0")
        if self.acquire_timeout_seconds < 0:
            raise ValueError("acquire_timeout_seconds 不能小于 0")


DEFAULT_EXTERNAL_SERVICE_POLICIES = {
    "quote": ExternalServicePolicy(
        timeout_seconds=30,
        max_attempts=2,
        max_concurrency=3,
        failure_threshold=3,
        recovery_seconds=30,
    ),
    "screener": ExternalServicePolicy(
        timeout_seconds=20,
        max_attempts=2,
        max_concurrency=3,
        failure_threshold=3,
        recovery_seconds=30,
    ),
    "news": ExternalServicePolicy(
        timeout_seconds=12,
        max_attempts=2,
        max_concurrency=3,
        failure_threshold=3,
        recovery_seconds=60,
    ),
    "ai": ExternalServicePolicy(
        timeout_seconds=30,
        max_attempts=2,
        max_concurrency=3,
        failure_threshold=3,
        recovery_seconds=60,
    ),
}


class ExternalServiceRuntime:
    """Bounded synchronous executor with retries, timeout and circuit breaker."""

    def __init__(
        self,
        name: str,
        policy: ExternalServicePolicy,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.name = name
        self.policy = policy
        self._clock = clock
        self._sleeper = sleeper
        self._executor = ThreadPoolExecutor(
            max_workers=policy.max_concurrency,
            thread_name_prefix=f"external-{name}",
        )
        self._slots = threading.BoundedSemaphore(policy.max_concurrency)
        self._lock = threading.Lock()
        self._metrics: Dict[str, Any] = {
            "requests": 0,
            "attempts": 0,
            "successes": 0,
            "failures": 0,
            "timeouts": 0,
            "retries": 0,
            "rejected": 0,
            "total_latency_ms": 0.0,
            "in_flight": 0,
            "max_in_flight": 0,
            "last_error": None,
        }
        self._consecutive_failures = 0
        self._open_until = 0.0
        self._half_open_in_flight = False

    def call(
        self,
        operation: str,
        callback: Callable[..., Any],
        *args,
        retry_if: Optional[Callable[[BaseException], bool]] = None,
        **kwargs,
    ) -> Any:
        started_at = self._clock()
        with self._lock:
            self._metrics["requests"] += 1

        try:
            self._before_request(operation)
        except ExternalServiceCircuitOpenError as exc:
            self._finish_failure(exc, started_at, circuit_failure=False)
            raise

        last_error: Optional[BaseException] = None
        for attempt in range(1, self.policy.max_attempts + 1):
            with self._lock:
                self._metrics["attempts"] += 1
            try:
                future = self._submit(operation, callback, *args, **kwargs)
                result = future.result(timeout=self.policy.timeout_seconds)
            except TimeoutError as exc:
                future.cancel()
                last_error = ExternalServiceTimeoutError(
                    f"{self.name}.{operation} 超过 "
                    f"{self.policy.timeout_seconds:g} 秒"
                )
                with self._lock:
                    self._metrics["timeouts"] += 1
                    self._metrics["last_error"] = str(last_error)
            except ExternalServiceBusyError as exc:
                last_error = exc
                break
            except Exception as exc:  # preserve SDK/network failures as cause
                last_error = exc
                with self._lock:
                    self._metrics["last_error"] = str(exc)
            else:
                self._finish_success(started_at)
                return result

            should_retry = (
                attempt < self.policy.max_attempts
                and not isinstance(last_error, ExternalServiceBusyError)
                and (retry_if(last_error) if retry_if else True)
            )
            if not should_retry:
                break
            with self._lock:
                self._metrics["retries"] += 1
            delay = self.policy.backoff_seconds * attempt
            if delay:
                self._sleeper(delay)

        assert last_error is not None
        if isinstance(last_error, ExternalServiceError):
            exposed_error = last_error
        else:
            exposed_error = ExternalServiceCallError(
                f"{self.name}.{operation} 调用失败: {last_error}"
            )
        self._finish_failure(exposed_error, started_at, circuit_failure=True)
        if exposed_error is last_error:
            raise exposed_error
        raise exposed_error from last_error

    def snapshot(self) -> Dict[str, Any]:
        now = self._clock()
        with self._lock:
            metrics = dict(self._metrics)
            if self._open_until > now:
                state = "open"
            elif self._open_until:
                state = "half_open"
            else:
                state = "closed"
            requests = metrics["requests"]
            return {
                **metrics,
                "avg_latency_ms": (
                    round(metrics["total_latency_ms"] / requests, 3)
                    if requests
                    else 0.0
                ),
                "failure_rate": (
                    round(metrics["failures"] / requests, 6)
                    if requests
                    else 0.0
                ),
                "circuit_state": state,
                "consecutive_failures": self._consecutive_failures,
                "open_for_seconds": (
                    round(max(0.0, self._open_until - now), 3)
                    if state == "open"
                    else 0.0
                ),
                "policy": {
                    "timeout_seconds": self.policy.timeout_seconds,
                    "max_attempts": self.policy.max_attempts,
                    "max_concurrency": self.policy.max_concurrency,
                    "failure_threshold": self.policy.failure_threshold,
                    "recovery_seconds": self.policy.recovery_seconds,
                },
            }

    def reset(self) -> None:
        with self._lock:
            for key in (
                "requests",
                "attempts",
                "successes",
                "failures",
                "timeouts",
                "retries",
                "rejected",
                "total_latency_ms",
            ):
                self._metrics[key] = 0
            self._metrics["max_in_flight"] = self._metrics["in_flight"]
            self._metrics["last_error"] = None
            self._consecutive_failures = 0
            self._open_until = 0.0
            self._half_open_in_flight = False

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _before_request(self, operation: str) -> None:
        now = self._clock()
        with self._lock:
            if self._open_until > now:
                self._metrics["rejected"] += 1
                raise ExternalServiceCircuitOpenError(
                    f"{self.name}.{operation} 熔断中，"
                    f"{self._open_until - now:.1f} 秒后重试"
                )
            if self._open_until:
                if self._half_open_in_flight:
                    self._metrics["rejected"] += 1
                    raise ExternalServiceCircuitOpenError(
                        f"{self.name}.{operation} 正在半开探测"
                    )
                self._half_open_in_flight = True

    def _submit(
        self,
        operation: str,
        callback: Callable[..., Any],
        *args,
        **kwargs,
    ) -> Future:
        acquired = self._slots.acquire(
            timeout=self.policy.acquire_timeout_seconds,
        )
        if not acquired:
            with self._lock:
                self._metrics["rejected"] += 1
            raise ExternalServiceBusyError(
                f"{self.name}.{operation} 并发已满"
            )

        with self._lock:
            self._metrics["in_flight"] += 1
            self._metrics["max_in_flight"] = max(
                self._metrics["max_in_flight"],
                self._metrics["in_flight"],
            )
        try:
            future = self._executor.submit(callback, *args, **kwargs)
        except Exception:
            self._release_slot()
            raise
        future.add_done_callback(lambda _future: self._release_slot())
        return future

    def _release_slot(self) -> None:
        with self._lock:
            self._metrics["in_flight"] -= 1
        self._slots.release()

    def _finish_success(self, started_at: float) -> None:
        elapsed_ms = (self._clock() - started_at) * 1000
        with self._lock:
            self._metrics["successes"] += 1
            self._metrics["total_latency_ms"] += elapsed_ms
            self._metrics["last_error"] = None
            self._consecutive_failures = 0
            self._open_until = 0.0
            self._half_open_in_flight = False

    def _finish_failure(
        self,
        error: BaseException,
        started_at: float,
        circuit_failure: bool,
    ) -> None:
        elapsed_ms = (self._clock() - started_at) * 1000
        with self._lock:
            self._metrics["failures"] += 1
            self._metrics["total_latency_ms"] += elapsed_ms
            self._metrics["last_error"] = str(error)
            if circuit_failure:
                self._consecutive_failures += 1
                if (
                    self._half_open_in_flight
                    or self._consecutive_failures
                    >= self.policy.failure_threshold
                ):
                    self._open_until = (
                        self._clock() + self.policy.recovery_seconds
                    )
            if circuit_failure:
                self._half_open_in_flight = False


class StockPickerRuntimeMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache_hits = 0
        self._cache_misses = 0
        self._cache_bypasses = 0
        self._ai_attempts = 0
        self._ai_available = 0
        self._ai_degraded = 0

    def record_cache(self, status: str) -> None:
        with self._lock:
            if status == "hit":
                self._cache_hits += 1
            elif status == "miss":
                self._cache_misses += 1
            elif status == "bypass":
                self._cache_bypasses += 1
            else:
                raise ValueError("cache status 必须是 hit、miss 或 bypass")

    def record_ai(self, degraded: bool) -> None:
        with self._lock:
            self._ai_attempts += 1
            if degraded:
                self._ai_degraded += 1
            else:
                self._ai_available += 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            cache_requests = self._cache_hits + self._cache_misses
            return {
                "cache": {
                    "requests": cache_requests,
                    "hits": self._cache_hits,
                    "misses": self._cache_misses,
                    "bypasses": self._cache_bypasses,
                    "hit_rate": (
                        round(self._cache_hits / cache_requests, 6)
                        if cache_requests
                        else 0.0
                    ),
                },
                "ai": {
                    "attempts": self._ai_attempts,
                    "available": self._ai_available,
                    "degraded": self._ai_degraded,
                    "degradation_rate": (
                        round(self._ai_degraded / self._ai_attempts, 6)
                        if self._ai_attempts
                        else 0.0
                    ),
                },
            }

    def reset(self) -> None:
        with self._lock:
            self._cache_hits = 0
            self._cache_misses = 0
            self._cache_bypasses = 0
            self._ai_attempts = 0
            self._ai_available = 0
            self._ai_degraded = 0


_external_service_runtimes = {
    name: ExternalServiceRuntime(name, policy)
    for name, policy in DEFAULT_EXTERNAL_SERVICE_POLICIES.items()
}
_stock_picker_metrics = StockPickerRuntimeMetrics()


def run_external_call(
    service: str,
    operation: str,
    callback: Callable[..., Any],
    *args,
    retry_if: Optional[Callable[[BaseException], bool]] = None,
    **kwargs,
) -> Any:
    try:
        runtime = _external_service_runtimes[service]
    except KeyError as exc:
        raise ValueError(f"未知外部服务: {service}") from exc
    return runtime.call(
        operation,
        callback,
        *args,
        retry_if=retry_if,
        **kwargs,
    )


def record_stock_picker_cache(status: str) -> None:
    _stock_picker_metrics.record_cache(status)


def record_stock_picker_ai(degraded: bool) -> None:
    _stock_picker_metrics.record_ai(degraded)


def get_stock_picker_reliability_snapshot() -> Dict[str, Any]:
    return {
        "scope": "process",
        "services": {
            name: runtime.snapshot()
            for name, runtime in _external_service_runtimes.items()
        },
        "stock_picker": _stock_picker_metrics.snapshot(),
        "limitations": [
            "指标、熔断和并发额度仅在当前服务进程内共享",
            "多 worker 或多实例部署需要外部指标系统和共享熔断状态",
        ],
    }


def reset_stock_picker_reliability_metrics() -> None:
    for runtime in _external_service_runtimes.values():
        runtime.reset()
    _stock_picker_metrics.reset()
