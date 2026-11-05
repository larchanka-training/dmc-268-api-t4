from dataclasses import dataclass, field


@dataclass
class Preferences:
    user_id: int
    email: bool = True
    sms: bool = False
    channels: list[str] = field(default_factory=list)
    quiet_hours: tuple[int, int] = (22, 7)
