"""Usage costs never charge each meal for an entire shared package."""
from copy import deepcopy

import pytest

from grocer.costs import for_servings, meal_costs


def example():
    return {"meals": [{"key": "a", "recipe_id": "r", "name": "Dinner", "servings": 2},
                       {"key": "b", "recipe_id": "r", "name": "Dinner again", "servings": 4}],
            "items": [{"key": "rice", "category": "pantry", "status": "covered", "qty": 0,
                       "meal_keys": ["a", "b"], "meal_usage": {"a": {"weight": 250}, "b": {"weight": 500}},
                       "pick": {"price": 8.0, "size_qty": 1000, "size_dim": "weight", "sales_unit": "EACH"}}]}


def test_shared_ingredients_and_pantry_cost_the_amount_used():
    costs = meal_costs(example())
    assert [c["total"] for c in costs] == [2, 4]
    assert [c["per_serving"] for c in costs] == [1, 1]
    assert for_servings(costs[0], 6)["total"] == 6


@pytest.mark.parametrize("change", [{"price": None}, {"price": -1}, {"price": float("nan")},
                                     {"size_qty": None}, {"sales_unit": "WEIGHT"}, {"size_dim": "count"}])
def test_unknown_prices_and_incompatible_units_are_incomplete(change):
    review = example()
    review["items"][0]["key"] = "unknown food"
    review["items"][0]["pick"].update(change)
    c = meal_costs(review)[0]
    assert c["total"] is None and c["per_serving"] is None
    assert "unknown food" in c["missing"]


def test_missing_quantities_unread_lines_and_yield_are_not_free():
    review = example()
    review["items"][0]["unquantified"] = True
    review["meals"][0].update(unparsed=["unread ingredient"], yield_question=True)
    c = meal_costs(review)[0]
    assert not c["complete"] and len(c["missing"]) == 3


def test_package_units_and_volume_conversions():
    review = example()
    review["items"][0]["meal_usage"]["a"] = {"pkg": 0.5}
    assert meal_costs(review)[0]["total"] == 4
    oil = review["items"][0]
    oil.update(key="oil", category="oil", meal_usage={"a": {"volume": 100}, "b": {"volume": 200}})
    oil["pick"].update(size_dim="volume", size_qty=1000)
    assert meal_costs(review)[0]["total"] == 0.8


def test_servings_scale_without_mutating_cached_estimate():
    c = meal_costs(example())[0]
    before = deepcopy(c)
    assert for_servings(c, 4)["total"] == 4
    assert c == before


def test_explicit_each_produce_uses_one_item_not_unknown_package_size():
    review = example()
    item = review["items"][0]
    item.update(key="yellow onion", category="produce", meal_usage={"a": {"count": 0.5}, "b": {"count": 1}})
    item["pick"].update(title="Yellow Onion, Each", price=1.2, size_qty=None, size_dim=None)
    assert meal_costs(review)[0]["total"] == 0.6
