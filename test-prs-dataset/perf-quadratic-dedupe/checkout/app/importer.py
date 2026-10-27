def unique_events(events: list[dict]) -> list[dict]:
    seen_ids: list[str] = []
    unique: list[dict] = []
    for event in events:
        if event["id"] not in seen_ids:
            seen_ids.append(event["id"])
            unique.append(event)
    return unique
