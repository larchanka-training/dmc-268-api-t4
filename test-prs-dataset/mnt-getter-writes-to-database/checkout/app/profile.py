from datetime import UTC, datetime


def get_display_name(session, user) -> str:
    user.last_seen = datetime.now(UTC)
    session.commit()
    return user.nickname or user.email.split("@")[0]
