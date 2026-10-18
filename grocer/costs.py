"""Estimated ingredient usage costs, independent of pantry and cart package rounding."""
from __future__ import annotations

import json
import math
import re

from . import units


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def meal_costs(review: dict) -> list[dict]:
    result = []
    for meal in review.get('meals', []):
        known, missing = 0.0, list(meal.get('unparsed', []))
        if not meal.get('key'):
            missing.append('Rebuild this list to estimate meal costs')
        for item in review.get('items', []):
            if not item.get('meal_keys') and meal.get('name') in item.get('sources', []):
                missing.append(item['key'])
            if meal.get('key') not in item.get('meal_keys', []) or item.get('category') == 'none':
                continue
            pick = item.get('pick') or {}
            price, size = pick.get('price'), pick.get('size_qty')
            dimension = pick.get('size_dim')
            if not size and re.search(r'\beach\b', pick.get('title') or '', re.I):
                size, dimension = 1.0, 'count'
            usage = item.get('meal_usage', {}).get(meal['key'], {})
            usable = (_number(price) and price >= 0 and _number(size) and size > 0
                      and dimension and pick.get('sales_unit') != 'WEIGHT'
                      and usage and not item.get('unquantified'))
            packages = 0.0
            if usable:
                for usage_dimension, quantity in usage.items():
                    if not _number(quantity) or quantity < 0:
                        usable = False
                        break
                    if usage_dimension == 'pkg':
                        packages += quantity
                    else:
                        converted = units.convert(units.Amount(quantity, usage_dimension), dimension,
                                                  item['key'], item.get('category', 'other'))
                        if converted is None:
                            usable = False
                            break
                        packages += converted.qty / size
            if usable:
                known += packages * price
            else:
                missing.append(item['key'])
        if meal.get('yield_question'):
            missing.append('Recipe servings need checking')
        servings = meal.get('servings')
        if not _number(servings) or servings <= 0:
            missing.append('Planned servings need checking')
        complete = not missing
        result.append({'key': meal.get('key'), 'recipe_id': meal.get('recipe_id'), 'name': meal.get('name', 'Dinner'),
                       'servings': servings, 'total': round(known, 2) if complete else None,
                       'per_serving': round(known / servings, 2) if complete else None,
                       'known_total': round(known, 2), 'missing': sorted(set(missing)),
                       'complete': complete, 'estimated_at': review.get('built')})
    return result


def saved_recipe_costs(db) -> dict:
    """Use saved review data only, without browser, model, or recipe requests."""
    result = {}
    for row in db.all('SELECT data FROM reviews ORDER BY id DESC LIMIT 100'):
        for cost in meal_costs(json.loads(row['data'])):
            result.setdefault(cost['recipe_id'], cost)
    return result


def for_servings(cost: dict | None, servings: float) -> dict | None:
    if not cost or not _number(cost.get('servings')) or cost['servings'] <= 0:
        return None
    factor = servings / cost['servings']
    return {**cost, 'servings': servings,
            'total': round(cost['total'] * factor, 2) if cost['total'] is not None else None,
            'known_total': round(cost['known_total'] * factor, 2)}
