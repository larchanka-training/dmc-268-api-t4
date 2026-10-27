def slowest(durations: list[float]) -> float:
    return max(durations, default=0.0)


def mean_duration(durations: list[float]) -> float:
    return sum(durations) / len(durations)
