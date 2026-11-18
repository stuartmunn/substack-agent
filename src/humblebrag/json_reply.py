"""Parses a model reply that should be a single JSON object."""

import json
import re
from typing import Any

_FENCE = re.compile(r"\A```(?:json)?\s*(?P<inner>.*?)\s*```\Z", re.DOTALL)


class JsonReplyError(ValueError):
    """The reply is not a single JSON object."""


def parse_json_object(reply: str) -> dict[str, Any]:
    """Return the JSON object in ``reply`` (a code fence is tolerated).

    Raises JsonReplyError if the reply is not valid JSON or not an object.
    """
    stripped = reply.strip()
    fenced = _FENCE.match(stripped)
    text = fenced["inner"] if fenced else stripped
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise JsonReplyError("reply is not valid JSON") from None
    if not isinstance(data, dict):
        raise JsonReplyError("reply is not a JSON object")
    return data
