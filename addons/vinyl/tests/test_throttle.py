"""The throttle keeps two calls at least ``min_interval`` apart, with an injected clock."""

from __future__ import annotations

from joshua_vinyl.throttle import Throttle


def test_the_first_call_never_waits() -> None:
    slept: list[float] = []
    throttle = Throttle(1.0, clock=lambda: 100.0, sleep=slept.append)
    assert throttle.wait() == 0.0
    assert slept == []


def test_a_fast_second_call_waits_for_the_remainder() -> None:
    clock = {"t": 0.0}
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += seconds

    throttle = Throttle(1.0, clock=lambda: clock["t"], sleep=sleep)
    throttle.wait()
    clock["t"] += 0.25
    assert throttle.wait() == 0.75
    assert slept == [0.75]


def test_a_slow_second_call_does_not_wait() -> None:
    clock = {"t": 0.0}
    slept: list[float] = []
    throttle = Throttle(1.0, clock=lambda: clock["t"], sleep=slept.append)
    throttle.wait()
    clock["t"] += 5
    assert throttle.wait() == 0.0
    assert slept == []
