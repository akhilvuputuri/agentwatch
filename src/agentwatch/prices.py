"""Model prices: what a model call costs when its exporter did not report a cost.

Like Langfuse's model definitions, a price matches model names by regular expression and can
take effect from a given time; the store applies the latest matching price to each model call
that has tokens but no reported cost.
"""

from __future__ import annotations

import math
import re
import time
import uuid
from typing import Any

from agentwatch.store import Price

MAX_PATTERN = 200
# A generous ceiling: no model costs anywhere near a million dollars per million tokens.
MAX_PER_MILLION = 1e6
MAX_TIME_NS = 2**63 - 1
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")


class PriceError(ValueError):
    """A price that cannot be stored, with the reason."""


def from_api(body: Any, *, now_ns: int | None = None) -> Price:
    if not isinstance(body, dict):
        raise PriceError("a price must be a JSON object")
    data: dict[str, Any] = body  # pyright: ignore[reportUnknownVariableType]
    pattern = data.get("match_pattern")
    if not isinstance(pattern, str) or not pattern.strip() or len(pattern) > MAX_PATTERN:
        raise PriceError(
            f"match_pattern must be a regular expression of 1 to {MAX_PATTERN} characters"
        )
    given_id = data.get("id")
    if given_id is None:
        price_id = uuid.uuid4().hex
    elif isinstance(given_id, str) and _ID.fullmatch(given_id) and given_id not in (".", ".."):
        price_id = given_id
    else:
        raise PriceError("id must be 1 to 200 letters, digits or . _ : - (not . or ..)")
    start = data.get("start_ns")
    start_ns: int | None
    if start is None:
        start_ns = None
    elif isinstance(start, str) and start.isascii() and start.isdigit() and len(start) <= 19:
        start_ns = int(start)
    elif isinstance(start, int) and not isinstance(start, bool):
        start_ns = start
    else:
        raise PriceError("start_ns must be Unix nanoseconds")
    if start_ns is not None and not 0 <= start_ns <= MAX_TIME_NS:
        raise PriceError("start_ns is out of range")
    return Price(
        id=price_id,
        match_pattern=pattern.strip(),
        input_per_million=_amount(data, "input_per_million", required=True) or 0.0,
        output_per_million=_amount(data, "output_per_million", required=True) or 0.0,
        cache_read_per_million=_amount(data, "cache_read_per_million"),
        cache_write_per_million=_amount(data, "cache_write_per_million"),
        start_ns=start_ns,
        created_ns=time.time_ns() if now_ns is None else now_ns,
    )


def _amount(data: dict[str, Any], key: str, *, required: bool = False) -> float | None:
    value = data.get(key)
    if value is None:
        if required:
            raise PriceError(f"{key} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PriceError(f"{key} must be a number of US dollars per million tokens")
    try:
        number = float(value)
    except OverflowError as error:
        raise PriceError(f"{key} is too large") from error
    if not math.isfinite(number) or not 0 <= number <= MAX_PER_MILLION:
        raise PriceError(f"{key} must be between 0 and {MAX_PER_MILLION:g}")
    return number
