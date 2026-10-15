"""Local LLM calls through Ollama. Used only for narrow judgement calls."""
from __future__ import annotations

import json
import re
import logging

import httpx

log = logging.getLogger(__name__)

CATEGORIES = ["produce", "meat", "seafood", "dairy", "bakery", "pantry", "spice",
              "condiment", "oil", "frozen", "other", "none"]


class LLM:
    def __init__(self, url: str, model: str):
        self.url = url
        self.model = model
        self.http = httpx.Client(timeout=180.0)

    def _chat(self, system: str, user: str, schema: dict, think: bool = False, max_tokens: int | None = None) -> dict:
        body = {
            "model": self.model,
            "stream": False,
            "think": think,
            "format": schema,
            # num_ctx is the same everywhere: a change makes Ollama reload the model
            "options": {"temperature": 0, "num_ctx": 4096, **({"num_predict": max_tokens} if max_tokens else {})},
            "keep_alive": "30m",      # stay loaded between picks; the GPU is shared with other models
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        r = self.http.post(f"{self.url}/api/chat", json=body)
        if r.status_code == 400 and "think" in r.text:
            body.pop("think")  # models without a thinking mode
            r = self.http.post(f"{self.url}/api/chat", json=body)
        r.raise_for_status()
        try:
            return json.loads(r.json()["message"]["content"])
        except (json.JSONDecodeError, KeyError, TypeError):
            if not think:
                raise
            # some Ollama versions don't mix thinking with a forced JSON shape; ask again plainly
            return self._chat(system, user, schema, think=False, max_tokens=max_tokens)

    def canonicalize(self, foods: list[str], existing: list[str] | None = None) -> list[dict]:
        """Map cleaned ingredient text to a generic grocery item + category, reusing existing names."""
        if not foods:
            return []
        schema = {
            "type": "object",
            "properties": {"items": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "input": {"type": "string"},
                    "item": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                },
                "required": ["input", "item", "category"],
            }}},
            "required": ["items"],
        }
        system = (
            "You turn recipe ingredient names into the generic grocery item a shopper would search for "
            "at Walmart. Rules: lowercase, singular, no brands, no preparation words. Keep distinctions "
            "that change what you buy (kosher salt vs table salt, shredded cheddar vs block cheddar, "
            "ground beef vs steak, corn tortilla vs flour tortilla). Never shorten a product to a generic word: "
            "bbq sauce stays bbq sauce (not sauce), soy sauce stays soy sauce, chicken thigh stays chicken thigh, "
            "garlic powder stays garlic powder. Expand abbreviations only if clearer. When the input offers alternatives "
            "('cheddar or colby-jack'), pick the first. Categories: fresh fruit, vegetables and fresh herbs "
            "(garlic, ginger, onion, scallion, lemon) are 'produce'; chicken, beef, pork, turkey, bacon, sausage are "
            "'meat'; fish and shellfish are 'seafood'; milk, cream, butter, cheese, yogurt and eggs are 'dairy'; bread, "
            "buns, tortillas are 'bakery'; frozen foods are 'frozen'; spices and dried herbs are 'spice'; cooking oils "
            "are 'oil'; sauces, vinegars, juices, honey and syrups are 'condiment'; flour, sugar, rice, pasta, canned "
            "and baking goods are 'pantry'. Use 'none' ONLY for water and ice; everything else is bought. "
            "Return one entry per input, same order. "
            "'existing' is just the names this household already uses for ingredients (not what they have in stock). "
            "If an input is the same product a "
            "household would buy for it, return that existing name exactly (cracked black pepper, freshly ground "
            "pepper -> black pepper; dried parsley leaves -> dried parsley; boneless chicken breast -> chicken breast). "
            "Do not merge different products: ground beef is not beef broth, crushed red pepper is not red bell "
            "pepper, fresh ginger is not ground ginger, salted butter is fine as butter but peanut butter is not."
        )
        user = json.dumps({"inputs": foods, "existing": (existing or [])[:300]})
        out = self._chat(system, user, schema)["items"]
        for o in out:   # only water and ice are never bought, whatever the model says
            if o.get("category") == "none" and not re.search(r"\b(?:water|ice)\b", f'{o.get("input", "")} {o.get("item", "")}'):
                o["category"] = "other"
        by_input = {o["input"].strip().lower(): o for o in out}
        result = []
        for n, f in enumerate(foods):
            o = by_input.get(f) or (out[n] if n < len(out) else None)
            if not o:
                o = {"item": f, "category": "other"}
            # always key the answer by our own text, never the model's echo of it
            result.append({"input": f, "item": (o.get("item") or "").strip().lower() or f,
                           "category": o.get("category") or "other"})
        return result

    def choose_product(self, food: str, need: str, candidates: list[dict], avoid: list[str] | None = None,
                       usual_brands: list[str] | None = None, history: bool = False,
                       recipe_lines: list[str] | None = None) -> dict:
        """Pick the best listing for a grocery item. Returns {'index': int, 'reason': str}; -1 if none fit.
        history=True: candidates are things the household bought before; only pick one that IS the item."""
        schema = {
            "type": "object",
            "properties": {"index": {"type": "integer"}, "reason": {"type": "string"}},
            "required": ["index", "reason"],
        }
        listing = []
        for i, c in enumerate(candidates):
            row = {"index": i, "title": c["title"], "price": c.get("price")}
            if c.get("unit_price"):
                row["unit_price"] = c["unit_price"]
            if not history:
                row["in_stock"] = c.get("in_stock")
                if c.get("bought_before"):
                    row["bought_before"] = True
                if (c.get("sales_unit") or "EACH").upper() != "EACH":
                    row["sold_by"] = c["sales_unit"]
            listing.append(row)
        if history:
            system = (
                "These are products this household has bought at Walmart before. A recipe needs the item below. "
                "Pick the listing that is that item, so the household gets the brand and product they already use. "
                "It must be the same kind of product a recipe asking for this would use (paprika can be any paprika, "
                "but chili powder is not paprika; shredded cheddar is not sliced cheddar; ground beef is not steak). "
                "Small differences in flavor or size are fine. Check it against the recipe lines: pasta sauce is "
                "not bbq sauce, peanut butter is not butter, seasoned salt is not salt. "
                "If none of them is the item, return index -1."
            )
            user = json.dumps({"item": food, "recipe_lines": (recipe_lines or [])[:6],
                               "needed_this_week": need, "previously_bought": listing})
        else:
            system = (
                "You pick one Walmart grocery listing for a household shopping list. It must be what the recipe "
                "lines call for. Prefer: the exact item "
                "(not a different product that mentions it), in stock, a normal household size close to what is "
                "needed, good unit price. Strongly prefer listings marked bought_before. Otherwise prefer the "
                "household's usual brands when the listing is the right item; store brand (Great Value, Marketside, "
                "Freshness Guaranteed) is fine. Avoid multipacks, bulk club sizes, snack or seasoning-mix variants, "
                "and non-food items. For fresh produce prefer the plain loose or bagged item. "
                "Return index -1 only if nothing fits."
            )
            user = json.dumps({"item": food, "recipe_lines": (recipe_lines or [])[:6], "needed_this_week": need,
                               "avoid": avoid or [], "usual_brands": usual_brands or [], "listings": listing})
        res = self._chat(system, user, schema)
        idx = int(res.get("index", -1))
        if idx >= len(candidates):
            idx = -1
        return {"index": idx, "reason": res.get("reason", "")}

    def judge_history(self, food: str, recipe_lines: list[str], candidates: list[dict]) -> list[dict]:
        """For the check page: a yes/no with a short reason for every past purchase considered."""
        if not candidates:
            return []
        schema = {
            "type": "object",
            "properties": {"verdicts": {"type": "array", "items": {
                "type": "object",
                "properties": {"index": {"type": "integer"}, "match": {"type": "boolean"}, "reason": {"type": "string"}},
                "required": ["index", "match", "reason"],
            }}},
            "required": ["verdicts"],
        }
        system = (
            "A household needs a grocery item. For EACH product they bought before, say whether it is that item "
            "(the same kind of product a recipe asking for it would use) and give a short reason, under 12 words. "
            "Small flavor or size differences are fine; a different product is not (pasta sauce is not bbq sauce, "
            "peanut butter is not butter, seasoned salt is not salt)."
        )
        user = json.dumps({"item": food, "recipe_lines": recipe_lines[:6],
                           "products": [{"index": i, "title": c["title"]} for i, c in enumerate(candidates)]})
        out = {v["index"]: v for v in self._chat(system, user, schema).get("verdicts", [])}
        return [out.get(i, {"index": i, "match": False, "reason": "no answer"}) for i in range(len(candidates))]

    def find_duplicates(self, names: list[str]) -> list[dict]:
        """Groups of ingredient names a household would buy as one product. [{"keep": name, "same": [names]}]"""
        if len(names) < 2:
            return []
        schema = {
            "type": "object",
            "properties": {"groups": {"type": "array", "items": {
                "type": "object",
                "properties": {"keep": {"type": "string", "maxLength": 60},
                               "same": {"type": "array", "maxItems": 6, "items": {"type": "string", "maxLength": 60}}},
                "required": ["keep", "same"],
            }, "maxItems": 10}},
            "required": ["groups"],
        }
        system = (
            "These are ingredient names from a household's recipes. Find names that are the SAME ingredient written "
            "differently, so any one of them could be bought for a recipe asking for another. Examples of the same: "
            "black pepper / ground black pepper / cracked black pepper; dried parsley / dried parsley leaves; "
            "green onion / scallion / spring onion; powdered sugar / confectioners' sugar; cilantro / fresh cilantro. "
            "NEVER group a kind, variety, flavor or form with the general word or with other kinds: cheese / cheddar "
            "cheese / mozzarella are all different; sugar / brown sugar / turbinado are different; squash / butternut "
            "squash / pumpkin are different; milk / coconut milk; oil / olive oil / grapeseed oil; potato / sweet "
            "potato; cod / salt cod; seeds / sesame seeds; beans / fava beans; butter / clarified butter. Also never "
            "group different products (garlic / garlic powder, fresh ginger / ground ginger, tomato / tomato sauce). "
            "When unsure, leave it out: a missed duplicate is harmless, a wrong one breaks recipes. Pick the plainest "
            "name as 'keep'. Only use names from the list, exactly as written. Usually there are few or none."
        )
        out = self._chat(system, json.dumps(names), schema, max_tokens=900).get("groups", [])
        valid = set(names)
        groups = []
        for g in out:
            keep = g.get("keep")
            same = [x for x in g.get("same", []) if x in valid and x != keep]
            if keep in valid and same:
                groups.append({"keep": keep, "same": same})
        return groups

    def confirm_same(self, pairs: list[tuple[str, str]]) -> list[dict]:
        """Second opinion, one pair at a time: would any 'b' from the store do when a recipe says 'a'?
        Returns [{"index", "reason", "same"}]; anything unanswered counts as not the same."""
        if not pairs:
            return []
        schema = {
            "type": "object",
            "properties": {"answers": {"type": "array", "items": {
                "type": "object",
                "properties": {"index": {"type": "integer"}, "reason": {"type": "string", "maxLength": 100},
                               "same": {"type": "boolean"}},
                "required": ["index", "reason", "same"],
            }, "maxItems": 20}},
            "required": ["answers"],
        }
        system = (
            "You check a grocery list cleanup. For EACH pair of ingredient names, decide if they are the same "
            "ingredient, so a recipe asking for A works with whatever you'd buy as B, AND a recipe asking for B "
            "works with whatever you'd buy as A. First write a short reason (under 15 words), then answer. "
            "Answer false when one is a kind, variety, flavor, cut or processed form of the other (cheddar cheese is "
            "a kind of cheese: false; brown sugar vs sugar: false; coconut milk vs milk: false; olive oil vs oils: "
            "false; salt cod vs cod: false; crushed tomatoes vs tomato: false; dried basil vs fresh basil: false). "
            "Answer true only for the same thing named differently (cracked black pepper vs black pepper: true; "
            "scallion vs green onion: true; dried parsley leaves vs dried parsley: true). If unsure, false."
        )
        user = json.dumps([{"index": i, "A": a, "B": b} for i, (a, b) in enumerate(pairs)])
        out = {v["index"]: v for v in self._chat(system, user, schema, max_tokens=1200).get("answers", [])
               if isinstance(v.get("index"), int)}
        return [out.get(i, {"index": i, "reason": "no answer", "same": False}) for i in range(len(pairs))]

