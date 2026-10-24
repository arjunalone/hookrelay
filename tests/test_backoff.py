"""
Retry backoff: the pure functions. No database needed.

This is the "preventing retry storms" area. The important property isn't
just that delays grow - it's that a delay is never SHORTER than its base.
Symmetric jitter would violate that and let a failed cohort re-synchronise.
"""

import pytest

from worker.backoff import (
    BASE_SCHEDULE,
    JITTER_RATIO,
    MAX_ATTEMPTS,
    get_delay_seconds,
    should_give_up,
)


@pytest.mark.parametrize("attempt,base", list(enumerate(BASE_SCHEDULE, start=1)))
def test_delay_never_falls_below_its_base(attempt, base):
    """The bug this guards against: symmetric (+/-) jitter.

    Random.uniform(-0.2, 0.2) can produce base * 0.8, i.e. a retry EARLIER
    than the schedule promises, which is what re-synchronises a stampede.
    """
    for _ in range(300):
        assert get_delay_seconds(attempt) >= base


def test_delay_never_exceeds_ceiling():
    """Jitter stays bounded, so retries can't drift arbitrarily far out."""
    for attempt in range(1, 7):
        base = BASE_SCHEDULE[min(attempt - 1, len(BASE_SCHEDULE) - 1)]
        ceiling = base * (1 + JITTER_RATIO)
        for _ in range(300):
            assert get_delay_seconds(attempt) <= ceiling


def test_delays_grow_with_attempts():
    """Later attempts wait longer than earlier ones, on average."""
    def mean(attempt):
        samples = [get_delay_seconds(attempt) for _ in range(400)]
        return sum(samples) / len(samples)

    assert mean(1) < mean(2) < mean(3) < mean(4) < mean(5)


def test_jitter_actually_varies():
    """Jitter that never varies is no jitter at all - it'd re-synchronise."""
    samples = {get_delay_seconds(1) for _ in range(50)}
    assert len(samples) > 1


def test_schedule_is_clamped_past_the_end():
    """Attempts beyond the schedule keep the last value rather than crashing."""
    for _ in range(50):
        assert get_delay_seconds(99) >= BASE_SCHEDULE[-1]


def test_give_up_boundary():
    """MAX_ATTEMPTS failures dead-letter the delivery; one fewer does not."""
    assert should_give_up(MAX_ATTEMPTS - 1) is False
    assert should_give_up(MAX_ATTEMPTS) is True
    assert should_give_up(MAX_ATTEMPTS + 1) is True
