"""Tests for how an importer states where a cost figure came from."""

from decimal import Decimal

import pytest

from metergraphrelay.billing_evidence import reported_cost


def test_an_amount_is_named_by_its_source():
    assert reported_cost(0.0012, source="langfuse.observation.totalCost") == {
        "reported_cost_usd": "0.0012",
        "reported_cost_source": "langfuse.observation.totalCost",
    }


def test_the_amount_keeps_every_digit_it_was_given():
    """A cost lands in a numeric column, so rounding introduced here survives
    the whole way."""
    assert reported_cost(
        Decimal("19.394766285"), source="portkey.cost"
    )["reported_cost_usd"] == "19.394766285"


@pytest.mark.parametrize("amount", [None, True, False, "", "abc", object()])
def test_a_source_is_never_named_without_an_amount_behind_it(amount):
    """A named source with no amount would read as a cost of nothing rather than
    as no cost reported."""
    assert reported_cost(amount, source="x.cost") == {}


@pytest.mark.parametrize("amount", [-1, "-0.01", float("inf"), float("nan")])
def test_an_amount_that_cannot_be_a_cost_is_refused(amount):
    assert reported_cost(amount, source="x.cost") == {}


def test_zero_is_a_cost():
    """Nothing billed is a fact; it is not a missing figure."""
    assert reported_cost(0, source="x.cost")["reported_cost_usd"] == "0"
