def percent_done(done_bytes: int, total_bytes: int) -> int:
    if total_bytes <= 0:
        return 0
    percent = done_bytes // total_bytes * 100
    return min(percent, 100)
