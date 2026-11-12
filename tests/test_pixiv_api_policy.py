"""Request-budget, response-error and Retry-After contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.pixiv_api_policy import (
    ApiBudgetExhausted,
    PixivApiRequestExecutor,
)


def _executor(*, budget=10, waits=None, max_attempts=3):
    waits = waits if waits is not None else []
    return PixivApiRequestExecutor(
        request_budget=budget,
        min_interval=0,
        wait=lambda seconds: waits.append(seconds) is None,
        should_stop=lambda: False,
        log=lambda _message: None,
        max_attempts=max_attempts,
        transient_delays=(1, 2),
        rate_limit_delays=(60, 120),
        jitter=0,
        clock=lambda: 0,
        random_uniform=lambda _low, _high: 0,
    )


def test_request_budget_counts_attempts() -> None:
    executor = _executor(budget=1)
    assert executor.call("ok", lambda: {"value": 1}) == {"value": 1}
    with pytest.raises(ApiBudgetExhausted):
        executor.call("blocked", lambda: None)
    assert executor.requests_used == 1


def test_retry_after_is_honoured_for_429() -> None:
    waits = []
    executor = _executor(waits=waits)
    calls = []

    class RateLimited(RuntimeError):
        header = {"Retry-After": "7"}
        status_code = 429

    def request():
        calls.append(1)
        if len(calls) == 1:
            raise RateLimited("429 Too Many Requests")
        return SimpleNamespace(value="ok")

    assert executor.call("作品分页", request).value == "ok"
    assert calls == [1, 1]
    assert waits == [7.0]


def test_api_error_payload_is_not_treated_as_empty_success() -> None:
    waits = []
    executor = _executor(waits=waits, max_attempts=2)
    calls = []

    def request():
        calls.append(1)
        if len(calls) == 1:
            return {"error": {"status": 503, "message": "temporarily unavailable"}}
        return {"illusts": []}

    assert executor.call("作品首页", request) == {"illusts": []}
    assert len(calls) == 2
    assert waits == [1.0]

