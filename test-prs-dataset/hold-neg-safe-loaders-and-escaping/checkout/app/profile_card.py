import hmac
import html

import yaml


def parse_settings(raw: str) -> dict:
    data = yaml.safe_load(raw)
    return data if isinstance(data, dict) else {}


def render_card(settings: dict) -> str:
    name = html.escape(str(settings.get("display_name", "anonymous")))
    return f"<div class=\"card\">{name}</div>"


def signature_ok(expected: str, received: str) -> bool:
    return hmac.compare_digest(expected.encode(), received.encode())
