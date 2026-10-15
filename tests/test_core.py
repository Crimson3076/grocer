"""Tests built from the household's real Mealie recipes."""
from datetime import date

import pytest

from grocer import units
from grocer.parse import parse_line
from grocer.planner import Need, decide, packages_needed

# (line, qty, unit, food) taken verbatim from recipes in Mealie
REAL_LINES = [
    ("1 tablespoon vegetable oil", 1, "tbsp", "vegetable oil"),
    ("1/2 medium yellow onion, diced", 0.5, None, "yellow onion"),
    ("1 jalapeño, seeded and diced", 1, None, "jalapeno"),
    ("4 cloves garlic, minced", 4, "clove", "garlic"),
    ("1 pound ground beef", 1, "lb", "ground beef"),
    ("Pinch cayenne", 1, "pinch", "cayenne"),
    ("1 plum or 1/4 cup grape tomatoes, chopped", 1, None, "plum tomato"),
    ("2 plum tomatoes or 1/2 cup grape tomatoes, seeded and diced", 2, None, "plum tomato"),
    ("1/2 cup chopped cilantro", 0.5, "cup", "cilantro"),
    ("Oil for frying", None, None, "oil"),
    ("12 corn tortillas", 12, None, "corn tortilla"),
    ("2 cups (8 ounces) shredded yellow cheddar or Colby-Jack cheese", 8, "oz", "shredded yellow cheddar cheese"),
    ("Salsa, for serving", None, None, "salsa"),
    ("4 (6 oz each) salmon filets", 24, "oz", "salmon filet"),
    ("1/4 tsp blackening seasoning (optional)", 0.25, "tsp", "blackening seasoning"),
    ("3 Tbsp butter", 3, "tbsp", "butter"),
    ("⅓ cup soy sauce ($0.44)", 1 / 3, "cup", "soy sauce"),
    ("1 ½ Tbsp cornstarch ($0.08)", 1.5, "tbsp", "cornstarch"),
    ("¾ lb. broccoli (1.75)", 0.75, "lb", "broccoli"),
    ("2 boneless, skinless chicken breasts (about 1.5 lb. total) ($6.25)", 2, None, "chicken breast"),
    ("3 Tbsp cooking oil, divided ($0.12)", 3, "tbsp", "cooking oil"),
    ("4 boneless, skinless chicken thighs (1.5lb, 680g, $3.52**)", 4, None, "chicken thigh"),
    ("4 pieces of Texas bread (thick sliced bread, $0.87)", 4, "piece", "texas bread"),
    ("8 slices cheddar cheese", 8, "slice", "cheddar cheese"),
    ("2 jalapeno peppers, sliced", 2, None, "jalapeno pepper"),
    ("3 pounds ground chuck", 3, "lb", "ground chuck"),
    ("³/₄ pound mozzarella cheese sliced", 0.75, "lb", "mozzarella cheese"),
]


@pytest.mark.parametrize("line,qty,unit,food", REAL_LINES)
def test_parse_real_lines(line, qty, unit, food):
    p = parse_line(line)
    if qty is None:
        assert p.qty is None
    else:
        assert p.qty == pytest.approx(qty, rel=0.01)
    assert p.unit == unit
    assert p.food == food


def test_optional_flag():
    assert parse_line("1/4 tsp blackening seasoning (optional)").optional


def test_package_sizes():
    assert units.package_size("Great Value Extra Virgin Olive Oil, 51 fl oz").qty == pytest.approx(51 * 29.57)
    assert units.package_size("Great Value 80/20 Ground Beef, 1 lb Tray").dim == "weight"
    assert units.package_size("Mission Corn Tortillas, 30 Count").qty == 30
    assert units.package_size("Hunt's Crushed Tomatoes, 28 oz Can").qty == pytest.approx(28 * 28.35)
    assert units.package_size("Fresh Yellow Onion, Each") is None  # treated as 1 each


def need(key, cat, *lines, factor=1.0):
    n = Need(key=key, category=cat)
    for line in lines:
        p = parse_line(line)
        a = p.amount()
        if a:
            a = units.Amount(a.qty * factor, a.dim)
        n.add(a, "test", line, p.optional)
    return n


def test_garlic_cloves_buy_one_bulb():
    n = need("garlic", "produce", "4 cloves garlic, minced", "6 cloves garlic (minced)")
    assert packages_needed(n, 1, "count", "EACH") == pytest.approx(1.0)  # 10 cloves = 1 bulb
    assert decide(n, {"size_qty": 1, "size_dim": "count"}, None, False) == ("buy", 1)


def test_tacos_for_two_people():
    # recipe makes 12 (tacos); scaled to 2 people with a 4-serving yield answer = factor 0.5
    n = need("corn tortilla", "bakery", "12 corn tortillas", factor=0.5)
    pick = {"size_qty": 30, "size_dim": "count", "sales_unit": "EACH"}
    assert decide(n, pick, None, False) == ("buy", 1)


def test_can_against_weight_listing():
    n = need("crushed tomato", "pantry", "1 can crushed tomatoes")
    assert packages_needed(n, 28 * 28.35, "weight", "EACH") == 1


def test_noodle_count_against_box():
    n = need("lasagna noodle", "pantry", "12 lasagna noodles", factor=2 / 12)
    assert packages_needed(n, 16 * 28.35, "weight", "EACH") == 1


def test_cilantro_volume_to_bunch():
    n = need("cilantro", "produce", "1/2 cup chopped cilantro")
    pk = packages_needed(n, 1, "count", "EACH")
    assert 0.2 < pk < 1.0


def test_olive_oil_with_stock():
    n = need("olive oil", "oil", "2 tsp olive oil")
    pick = {"size_qty": 51 * 29.57, "size_dim": "volume"}
    assert decide(n, pick, None, False) == ("check", 0)       # unknown stock: ask
    assert decide(n, pick, 0.5, False) == ("covered", 0)      # half a bottle left
    assert decide(n, pick, 0.0, False) == ("buy", 1)          # out


def test_oil_for_frying_unquantified():
    n = need("vegetable oil", "oil", "Oil for frying")
    pick = {"size_qty": 48 * 29.57, "size_dim": "volume"}
    assert decide(n, pick, None, False) == ("check", 0)
    assert decide(n, pick, 0.25, False) == ("buy", 1)


def test_staple_only_when_low():
    n = need("salt", "spice", "1 teaspoon kosher salt")
    assert decide(n, None, None, True) == ("staple", 0)
    assert decide(n, None, 0.0, True) == ("buy", 1)


def test_salmon_two_of_four_filets():
    n = need("salmon", "seafood", "4 (6 oz each) salmon filets", factor=2 / 4)
    pick = {"size_qty": 12 * 28.35, "size_dim": "weight"}
    assert decide(n, pick, None, False) == ("buy", 1)


def test_default_order_end():
    from grocer.dinners import _default_end
    assert _default_end(date(2026, 10, 5)) == date(2026, 10, 10)    # Monday: rest of this week
    assert _default_end(date(2026, 10, 9)) == date(2026, 10, 17)    # Friday: too short, through next week
    assert _default_end(date(2026, 10, 11)) == date(2026, 10, 17)   # Sunday: the week


@pytest.mark.parametrize("line,foods", [
    ("salt & pepper", ["salt", "pepper"]),
    ("1 tsp salt + pepper", ["salt", "pepper"]),
    ("salt/pepper", ["salt", "pepper"]),
    ("salt and/or pepper", ["salt", "pepper"]),
    ("half & half, for serving", ["half and half"]),
    ("1 cup half and half", ["half and half"]),
    ("mac & cheese", ["mac and cheese"]),
    ("butter or margarine", ["butter"]),
    ("chicken thighs/breasts", ["chicken thigh"]),
    ("garlic powder, onion powder and paprika", ["garlic powder", "onion powder", "paprika"]),
    ("1 onion, peeled and diced", ["onion"]),
    ("tomatoes, diced and drained", ["tomato"]),
    ("1 lb ground beef or ground turkey", ["ground beef"]),
    ("2 pounds chicken thighs, bone-in, skin-on", ["chicken thigh"]),
    ("1 1/2 pounds boneless, skinless chicken breasts, cut into 1-inch pieces", ["chicken breast"]),
    ("2 cups shredded cheddar or colby jack cheese", ["shredded cheddar cheese"]),
])
def test_pairs_symbols_and_choices(line, foods):
    from grocer.planner import split_pairs
    assert [p.food for p in split_pairs(parse_line(line))] == foods
