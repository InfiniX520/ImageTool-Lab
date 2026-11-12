"""Cancellation-aware request policy for long-running Pixiv crawl jobs."""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

_STATUS_PATTERN = re.compile(r"(?<!\d)(401|403|404|408|425|429|500|502|503|504)(?!\d)")
_SECRET_PATTERN = re.compile(
    r"(?i)(authorization|cookie|refresh[_ -]?token|access[_ -]?token)\s*[:=]\s*([^\s,;]+)"
)


def redact_error_message(value: object, limit: int = 500) -> str:
    """Return a bounded message with common credential forms removed."""

    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    return text[: max(0, int(limit))]


@dataclass(frozen=True, slots=True)
class ApiFailure:
    kind: str
    message: str
    status_code: int | None = None
    retry_after: float | None = None
    retryable: bool = True


class FollowCrawlControl(RuntimeError):
    """Base class for deliberate crawl pauses that must not be swallowed."""

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.retry_after = retry_after

    @property
    def retry_at(self) -> str:
        if not self.retry_after:
            return ""
        target = datetime.now(timezone.utc) + timedelta(seconds=max(0.0, self.retry_after))
        return target.isoformat(timespec="seconds")


class ApiBudgetExhausted(FollowCrawlControl):
    pass


class ApiRequestCancelled(FollowCrawlControl):
    pass


class ApiAuthenticationRequired(FollowCrawlControl):
    pass


class ApiRequestExhausted(FollowCrawlControl):
    pass


class RateLimitCircuitOpen(FollowCrawlControl):
    pass


class _ApiResponseError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _mapping_value(value: object, key: str):
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _response_error(result: object) -> _ApiResponseError | None:
    error = _mapping_value(result, "error")
    if not error:
        return None
    status = None
    for key in ("status", "status_code", "code"):
        raw = _mapping_value(error, key)
        try:
            status = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            status = None
        if status:
            break
    parts = []
    for key in ("user_message", "message", "reason"):
        value = _mapping_value(error, key)
        if value:
            parts.append(str(value))
    message = " | ".join(dict.fromkeys(parts)) or str(error)
    return _ApiResponseError(message, status_code=status)


def _status_from_exception(exc: BaseException, message: str) -> int | None:
    for candidate in (exc, getattr(exc, "response", None)):
        if candidate is None:
            continue
        for attribute in ("status_code", "status", "code"):
            raw = getattr(candidate, attribute, None)
            try:
                if raw is not None:
                    return int(raw)
            except (TypeError, ValueError):
                continue
    match = _STATUS_PATTERN.search(message)
    return int(match.group(1)) if match else None


def _retry_after_seconds(exc: BaseException) -> float | None:
    headers = getattr(exc, "header", None) or getattr(exc, "headers", None)
    if headers is None:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except AttributeError:
        return None
    if raw in (None, ""):
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(str(raw))
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def classify_api_failure(exc: BaseException) -> ApiFailure:
    body = getattr(exc, "body", "") or ""
    message = redact_error_message(f"{exc} {body}".strip())
    lowered = message.casefold()
    status = _status_from_exception(exc, message)
    retry_after = _retry_after_seconds(exc)

    if (
        status == 429
        or "too many requests" in lowered
        or "rate limit" in lowered
        or "限流" in lowered
    ):
        return ApiFailure("rate_limited", message, 429, retry_after, True)
    auth_markers = (
        "authentication",
        "invalid_grant",
        "access token",
        "invalid token",
        "please login",
        "login required",
    )
    if status in (401, 403) or any(marker in lowered for marker in auth_markers):
        return ApiFailure("authentication", message, status, retry_after, False)
    if status == 404:
        return ApiFailure("not_found", message, status, retry_after, False)
    transient_markers = (
        "timeout",
        "timed out",
        "connection",
        "unexpected_eof",
        "unexpected eof",
        "ssl",
        "temporarily unavailable",
        "remote disconnected",
        "reset by peer",
        "parse_json",
    )
    if status in (408, 425, 500, 502, 503, 504) or any(
        marker in lowered for marker in transient_markers
    ):
        return ApiFailure("transient", message, status, retry_after, True)
    return ApiFailure("unknown", message, status, retry_after, True)


class PixivApiRequestExecutor:
    """Apply pacing, a request budget and status-aware retries to API calls."""

    def __init__(
        self,
        *,
        request_budget: int,
        min_interval: float,
        wait: Callable[[float], bool],
        should_stop: Callable[[], bool],
        log: Callable[[str], None],
        max_attempts: int = 4,
        transient_delays: tuple[float, ...] = (5.0, 15.0, 30.0),
        rate_limit_delays: tuple[float, ...] = (60.0, 120.0, 300.0),
        jitter: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.request_budget = max(1, int(request_budget))
        self.min_interval = max(0.0, float(min_interval))
        self.wait = wait
        self.should_stop = should_stop
        self.log = log
        self.max_attempts = max(1, int(max_attempts))
        self.transient_delays = tuple(max(0.0, float(value)) for value in transient_delays) or (
            0.0,
        )
        self.rate_limit_delays = tuple(max(0.0, float(value)) for value in rate_limit_delays) or (
            60.0,
        )
        self.jitter = max(0.0, float(jitter))
        self.clock = clock
        self.random_uniform = random_uniform
        self.requests_used = 0
        self._last_request_started: float | None = None

    @property
    def requests_remaining(self) -> int:
        return max(0, self.request_budget - self.requests_used)

    def call(self, operation: str, request: Callable[[], object]):
        last_failure: ApiFailure | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._ensure_active()
            self._reserve_request(operation)
            self._pace()
            try:
                result = request()
                response_error = _response_error(result)
                if response_error is not None:
                    raise response_error
                return result
            except FollowCrawlControl:
                raise
            except Exception as exc:
                failure = classify_api_failure(exc)
                last_failure = failure
                if failure.kind == "authentication":
                    raise ApiAuthenticationRequired(
                        f"{operation} 需要重新登录：{failure.message}",
                        kind=failure.kind,
                        status_code=failure.status_code,
                    ) from exc
                if not failure.retryable:
                    raise ApiRequestExhausted(
                        f"{operation} 失败：{failure.message}",
                        kind=failure.kind,
                        status_code=failure.status_code,
                    ) from exc
                if attempt >= self.max_attempts:
                    break
                delay = self._retry_delay(failure, attempt)
                self.log(
                    f"[!] {operation} 请求失败，{delay:.0f} 秒后重试 "
                    f"({attempt}/{self.max_attempts - 1})：{failure.message}"
                )
                if not self.wait(delay):
                    raise ApiRequestCancelled("任务已停止", kind="cancelled") from exc

        assert last_failure is not None
        retry_after = self._retry_delay(last_failure, self.max_attempts)
        error_type = (
            RateLimitCircuitOpen if last_failure.kind == "rate_limited" else ApiRequestExhausted
        )
        raise error_type(
            f"{operation} 重试耗尽：{last_failure.message}",
            kind=last_failure.kind,
            status_code=last_failure.status_code,
            retry_after=retry_after,
        )

    def _ensure_active(self) -> None:
        if self.should_stop():
            raise ApiRequestCancelled("任务已停止", kind="cancelled")
        if self.requests_used >= self.request_budget:
            raise ApiBudgetExhausted(
                f"本批 API 请求预算已用完（{self.requests_used}/{self.request_budget}）",
                kind="request_budget",
            )

    def _reserve_request(self, operation: str) -> None:
        if self.requests_used >= self.request_budget:
            raise ApiBudgetExhausted(
                f"{operation} 尚未执行；本批 API 请求预算已用完（{self.requests_used}/{self.request_budget}）",
                kind="request_budget",
            )
        self.requests_used += 1

    def _pace(self) -> None:
        now = self.clock()
        if self._last_request_started is not None:
            interval = self.min_interval + self.random_uniform(0.0, self.jitter)
            remaining = interval - (now - self._last_request_started)
            if remaining > 0 and not self.wait(remaining):
                raise ApiRequestCancelled("任务已停止", kind="cancelled")
            now = self.clock()
        self._last_request_started = now

    def _retry_delay(self, failure: ApiFailure, attempt: int) -> float:
        if failure.retry_after is not None:
            return max(0.0, failure.retry_after)
        delays = self.rate_limit_delays if failure.kind == "rate_limited" else self.transient_delays
        return delays[min(max(0, int(attempt) - 1), len(delays) - 1)] + self.random_uniform(
            0.0, self.jitter
        )


__all__ = [
    "ApiAuthenticationRequired",
    "ApiBudgetExhausted",
    "ApiFailure",
    "ApiRequestCancelled",
    "ApiRequestExhausted",
    "FollowCrawlControl",
    "PixivApiRequestExecutor",
    "RateLimitCircuitOpen",
    "classify_api_failure",
    "redact_error_message",
]
