"""Per-model pricing, in one place, versioned.

Prices change. When they do, historical cost figures computed with the new
price are wrong, and a cost trend that moved because a vendor changed a rate
looks exactly like a cost trend that moved because your system got worse.

So prices carry an effective date and the cost function takes the time of the
call. Slightly annoying, and it means a cost chart from six months ago still
reads correctly.
"""
from __future__ import annotations

import bisect
import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    effective_from: dt.date
    prompt_per_1k: float
    completion_per_1k: float


# Ordered by effective_from within each model.
PRICES: dict[str, list[Price]] = {
    "triage-classifier-small": [
        Price(dt.date(2025, 1, 1), 0.00015, 0.00060),
        Price(dt.date(2025, 9, 1), 0.00010, 0.00040),
    ],
    "triage-drafter-large": [
        Price(dt.date(2025, 1, 1), 0.00300, 0.01500),
        Price(dt.date(2025, 9, 1), 0.00250, 0.01000),
    ],
    "judge-large": [
        Price(dt.date(2025, 1, 1), 0.00300, 0.01500),
    ],
}


class UnknownModel(KeyError):
    """Raised rather than defaulting to zero.

    A zero default means an unpriced model silently contributes nothing to the
    cost report, and the first sign of trouble is a bill that does not match
    the dashboard. Failing loud here costs one deploy; failing quiet costs a
    quarter of wrong numbers.
    """


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int,
             at: dt.date | None = None) -> float:
    at = at or dt.date.today()
    history = PRICES.get(model)
    if not history:
        raise UnknownModel(f"no pricing for model {model!r}")

    dates = [p.effective_from for p in history]
    index = bisect.bisect_right(dates, at) - 1
    if index < 0:
        raise UnknownModel(f"no price for {model!r} effective on {at}")
    price = history[index]
    return (prompt_tokens / 1000.0 * price.prompt_per_1k
            + completion_tokens / 1000.0 * price.completion_per_1k)
