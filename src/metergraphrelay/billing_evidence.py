"""How an importer states where a cost figure came from.

metergraph selects an effective cost from its own catalog and from amounts a
source reported. It can only weigh a reported amount if it knows what produced
it, because the field carrying one is open: a figure a customer's own client
wrote and a figure a gateway billed arrive in the same place and look alike.

Two kinds of source report a cost, and they are not equivalent:

* A **gateway** sits in the request path and states what the call cost. Naming
  it lets that amount be treated as billing evidence.
* An **observability platform** reports its own estimate, computed from the
  tokens it observed against a price table it maintains. Braintrust calls the
  field ``estimated_cost``, which is the honest description of all of them.

So an observability platform names its source and no gateway, while a gateway
states itself on the row. The amount is still recorded either way, and an audit
can compare it against a computed cost, but nothing can mistake an estimate for
something the provider charged.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def reported_cost(amount: Any, *, source: str) -> dict:
    """The fields naming a reported cost, or nothing when there is no amount.

    The amount is carried as a decimal string. A cost lands in a numeric column,
    and a value divided or parsed as a binary float arrives carrying rounding
    that no later step can remove.
    """
    if amount is None or isinstance(amount, bool):
        return {}
    try:
        parsed = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        return {}
    if not parsed.is_finite() or parsed < 0:
        return {}
    return {"reported_cost_usd": str(parsed), "reported_cost_source": source}
