from dataclasses import dataclass


@dataclass
class Ticket:
    id: int
    subject: str


def add_tag(ticket: Ticket, tag: str, tags: list[str] = []) -> list[str]:
    tags.append(f"{ticket.id}:{tag}")
    return tags
