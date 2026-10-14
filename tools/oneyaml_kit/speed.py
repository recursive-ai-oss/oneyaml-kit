"""How much longer than the stated time bound a case may take on this machine.

A bounded case states its time bound in seconds on a reference machine. A slower or busier
machine (a two-core CI runner running several jobs at once) would otherwise fail a library for
the machine's speed, and the same library would pass on a fast one. The bound is therefore
multiplied by a factor that is measured on the spot: how long a fixed piece of work takes here
compared with the reference. The factor is never below 1, so a fast machine is held to the
stated bound, and it moves in coarse steps (1, 2, 4, 8) so that noise in the measurement does
not change the bound from one case to the next. A bound that is still passed at the largest
factor is a hang, a leak or a runaway cost on any machine.

KIT_TIME_SCALE fixes the factor (a positive number) and skips the measurement; the kit's own
tests use it so that their timings do not depend on the machine.
"""

from __future__ import annotations

import os
import statistics
import time

# Seconds the probe takes on the reference machine. It is a little above what a fast desktop
# core needs, so that such a machine measures a factor of exactly 1.
REFERENCE_SECONDS = 0.024
STEPS = (1, 2, 4, 8)
# A measurement is reused for this many seconds: the load changes slowly, the probe costs time.
FRESH_SECONDS = 3.0

_cache: tuple[float, float] | None = None


def probe() -> float:
    """Seconds a fixed amount of pure computation takes now (the median of three runs, so one
    unlucky slice does not decide). It measures the machine as the adapters see it: busy cores
    and slow cores both make it longer."""
    runs = []
    for _ in range(3):
        t = time.perf_counter()
        s = 0
        for i in range(400_000):
            s += i * i % 7
        runs.append(time.perf_counter() - t)
    return statistics.median(runs)


def factor_for(seconds: float) -> float:
    """The smallest step that covers a probe time, in units of the reference."""
    ratio = seconds / REFERENCE_SECONDS
    for s in STEPS:
        if ratio <= s:
            return float(s)
    return float(STEPS[-1])


def fixed() -> float | None:
    v = os.environ.get("KIT_TIME_SCALE")
    if not v:
        return None
    f = float(v)
    if f <= 0:
        raise ValueError("KIT_TIME_SCALE must be a positive number")
    return f


def time_scale() -> float:
    """The factor to multiply a case's time bound by right now."""
    global _cache
    f = fixed()
    if f is not None:
        return f
    now = time.monotonic()
    if _cache is None or now - _cache[0] > FRESH_SECONDS:
        _cache = (now, factor_for(probe()))
    return _cache[1]
