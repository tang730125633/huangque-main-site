"""Shared duration-band helpers for short-drama planning and assembly."""


DURATION_BANDS = {
    30: (15, 30),
    45: (30, 60),
    60: (60, 90),
}
SHOT_DURATION_SECONDS = (5, 10)


def bounds(value):
    """Return the inclusive duration band represented by a stored legacy value."""
    try:
        key = int(value)
    except (TypeError, ValueError):
        key = 30
    if key in DURATION_BANDS:
        return DURATION_BANDS[key]
    # Internal tests and older imported contracts may carry an exact duration
    # outside the public presets. Preserve that value instead of silently
    # reinterpreting it as the 15-30 second band.
    if key > 0:
        return key, key
    return DURATION_BANDS[30]


def label(value):
    lower, upper = bounds(value)
    return "%d-%d 秒" % (lower, upper)


def contains(value, seconds):
    lower, upper = bounds(value)
    try:
        actual = int(seconds)
    except (TypeError, ValueError):
        return False
    return lower <= actual <= upper


def reachable_totals(value, shot_count):
    """Return every in-band total reachable with 5/10 second shots."""
    lower, upper = bounds(value)
    try:
        count = int(shot_count)
    except (TypeError, ValueError):
        return ()
    if count <= 0:
        return ()
    minimum = count * min(SHOT_DURATION_SECONDS)
    maximum = count * max(SHOT_DURATION_SECONDS)
    step = max(SHOT_DURATION_SECONDS) - min(SHOT_DURATION_SECONDS)
    return tuple(
        total for total in range(minimum, maximum + 1, step)
        if lower <= total <= upper
    )


def is_reachable(value, shot_count):
    return bool(reachable_totals(value, shot_count))


def choose(value, shot_count=0, authored_seconds=0, speech_seconds=0):
    """Choose the closest natural total that 5/10 second shots can reach."""
    totals = reachable_totals(value, shot_count)
    if not totals:
        raise ValueError("时长与分镜数量不匹配，无法组成 5/10 秒分镜")
    candidates = []
    for item in (authored_seconds, speech_seconds):
        try:
            candidates.append(max(0, int(round(float(item)))))
        except (TypeError, ValueError):
            pass
    count = max(0, int(shot_count))
    # Six seconds per shot is the default preference, but the returned value
    # must stay on the discrete 5/10-second lattice used by plan validation.
    preferred = max(candidates or [count * 6])
    return min(totals, key=lambda total: (abs(total - preferred), total))


def allocate(value, shot_count=0, authored_seconds=0, speech_seconds=0):
    """Return one deterministic 5/10-second allocation inside the band.

    Longer shots are placed later so the ending has room to resolve.  Callers
    that need a different dramatic order may reorder the returned durations,
    but must preserve this multiset.
    """
    count = int(shot_count)
    total = choose(value, count, authored_seconds, speech_seconds)
    long_count, remainder = divmod(total - count * 5, 5)
    if remainder or long_count < 0 or long_count > count:
        raise ValueError("时长与分镜数量不匹配，无法组成 5/10 秒分镜")
    return tuple([5] * (count - long_count) + [10] * long_count)


def contains_milliseconds(value, milliseconds, tolerance_ms=0):
    """Return whether a physical duration lies in the selected public band."""
    try:
        actual = int(milliseconds)
        tolerance = max(0, int(tolerance_ms))
    except (TypeError, ValueError):
        return False
    lower, upper = bounds(value)
    return lower * 1000 - tolerance <= actual <= upper * 1000 + tolerance
