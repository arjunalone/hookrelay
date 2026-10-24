"""
MODULE: worker/backoff.py
WHAT IT DOES: Calculates how long to wait before the next retry attempt,
              and whether a delivery should be given up on (dead-lettered).
PROBLEM SOLVED: Retrying instantly and forever wastes resources and can
                 hammer a struggling receiver, making things worse. Backoff
                 spaces out retries; a max-attempts cutoff stops forever-loops.
DATA SOURCE: Pure function - takes attempt_count, returns a delay in seconds.
             No DB access here; worker.py calls this and writes the result.
"""

import random

MAX_ATTEMPTS = 5  # after this many failures, mark as 'dead' instead of retrying

# Backoff schedule in seconds, indexed by attempt number (1st failure, 2nd, ...)
# Kept short for local testing - in production these would be minutes/hours
# (e.g. 60, 300, 1800, 7200 for 1min/5min/30min/2hr).
BASE_SCHEDULE = [10, 30, 60, 120, 300]

# Jitter is added ON TOP of the base delay, never subtracted from it.
JITTER_RATIO = 0.2


def get_delay_seconds(attempt_count):
    """
    attempt_count = how many times this delivery has already been attempted.
    Returns delay in seconds before the NEXT attempt is allowed, always >= base.

    Why the jitter is one-directional: symmetric jitter (+/- 20%) lets a retry
    fire EARLIER than the schedule says, which is the opposite of what prevents
    a retry storm. If a hundred deliveries fail against the same endpoint at
    the same moment, half of them would retry early and pull the cohort back
    into sync. Spreading only upwards stretches a stampede out instead.
    """
    index = min(attempt_count - 1, len(BASE_SCHEDULE) - 1)
    base = BASE_SCHEDULE[index]
    return max(1, base + base * random.uniform(0, JITTER_RATIO))


def should_give_up(attempt_count):
    return attempt_count >= MAX_ATTEMPTS