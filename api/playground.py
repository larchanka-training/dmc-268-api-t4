"""The rules playground (issue #53): try a `.review/rules.md` before committing it.

`POST /playground/rules/parse` parses a draft rules file with the parser reviews use
(`RulesParser`), so the console's editor shows exactly the rules, ignore directives and
dropped items a review would see. It calls no model and stores nothing. The draft's
text is returned to the caller who sent it and never logged.
"""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response

from adapters.llm.rules import IGNORE_MAX, RULES_MAX, RULES_MAX_CHARS
from api.deps import NO_STORE, ApiError, current_session, services
from domain.auth import Session
from domain.repository_rules import RULES_MAX_BYTES, ParsedRules

router = APIRouter()

# A JSON string can take up to six bytes per character of the text it carries (\uXXXX),
# so the body may be that much larger than the file it holds.
BODY_MAX_BYTES = 6 * RULES_MAX_BYTES + 1024


@router.post("/playground/rules/parse")
async def parse_playground_rules(
    request: Request,
    response: Response,
    _session: Annotated[Session, Depends(current_session)],
) -> dict[str, Any]:
    """Body `{"rules_markdown": "<text>"}`. 413 `rules_too_large` past the 64 KB a
    review accepts (docs/RULES_FORMAT_SPEC.md §4), 400 `invalid_body` otherwise."""
    markdown = _rules_markdown(await _read_body(request))
    if len(markdown.encode("utf-8")) > RULES_MAX_BYTES:
        raise ApiError(413, "rules_too_large")
    response.headers["Cache-Control"] = NO_STORE
    return _parsed_json(services(request).rules_parser.parse(markdown))


async def _read_body(request: Request) -> bytes:
    """The body, refused as soon as it passes the cap rather than read whole first."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > BODY_MAX_BYTES:
        raise ApiError(413, "rules_too_large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > BODY_MAX_BYTES:
            raise ApiError(413, "rules_too_large")
    return bytes(body)


def _rules_markdown(body: bytes) -> str:
    try:
        payload = json.loads(body)
    except ValueError:
        raise ApiError(400, "invalid_body") from None
    markdown = payload.get("rules_markdown") if isinstance(payload, dict) else None
    if not isinstance(markdown, str):
        raise ApiError(400, "invalid_body")
    try:
        # A lone surrogate (`"\ud800"`) is valid JSON but not UTF-8 text: §4 strict UTF-8.
        markdown.encode("utf-8")
    except UnicodeEncodeError:
        raise ApiError(400, "invalid_body") from None
    return markdown


def _parsed_json(parsed: ParsedRules) -> dict[str, Any]:
    return {
        "rules": [
            {
                "id": rule.id,
                "section": rule.section,
                "category": None if rule.category is None else rule.category.value,
                "text": rule.text,
                "line_start": rule.line_start,
                "line_end": rule.line_end,
            }
            for rule in parsed.rules
        ],
        "ignore": [
            {
                "kind": directive.kind.value,
                "value": directive.value,
                "line_start": directive.line_start,
                "line_end": directive.line_end,
            }
            for directive in parsed.ignore
        ],
        "diagnostics": [
            {
                "code": diagnostic.code.value,
                "dropped": diagnostic.code.dropped,
                "line_start": diagnostic.line_start,
                "line_end": diagnostic.line_end,
            }
            for diagnostic in parsed.diagnostics
        ],
        "dropped_suspicious": parsed.dropped_suspicious,
        "dropped_over_limit": parsed.dropped_over_limit,
        "limits": {
            "bytes": RULES_MAX_BYTES,
            "rules": RULES_MAX,
            "rule_chars": RULES_MAX_CHARS,
            "ignore": IGNORE_MAX,
        },
    }
