"""Minimal Mealie API client (v2/v3 household API, with v1 group fallback)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx


@dataclass
class Ingredient:
    text: str                 # best human-readable line
    qty: float | None = None  # only when Mealie has it parsed
    unit: str | None = None
    food: str | None = None


@dataclass
class Recipe:
    id: str
    slug: str
    name: str
    servings: float | None
    yield_text: str
    ingredients: list[Ingredient]
    last_made: str | None
    rating: float | None


@dataclass
class PlanEntry:
    date: str
    entry_type: str
    recipe_id: str | None
    recipe_slug: str | None
    title: str
    id: str | None = None     # Mealie's id for this plan entry (needed to swap it)


class Mealie:
    def __init__(self, url: str, token: str):
        self.http = httpx.Client(
            base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=30.0
        )
        self._household_slug: str | None = None

    def _get(self, path: str, **params) -> dict:
        r = self.http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    # ---- meal plan ----
    def mealplan(self, start: date, end: date) -> list[PlanEntry]:
        params = {"start_date": start.isoformat(), "end_date": end.isoformat(), "perPage": -1, "page": 1}
        try:
            data = self._get("/api/households/mealplans", **params)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                raise
            data = self._get("/api/groups/mealplans", **params)
        items = data if isinstance(data, list) else data.get("items", [])
        out = []
        for it in items:
            rec = it.get("recipe") or {}
            out.append(PlanEntry(
                date=it.get("date"),
                entry_type=(it.get("entryType") or it.get("entry_type") or "dinner").lower(),
                recipe_id=it.get("recipeId") or rec.get("id"),
                recipe_slug=rec.get("slug"),
                title=rec.get("name") or it.get("title") or "",
                id=str(it["id"]) if it.get("id") is not None else None,
            ))
        return out

    def add_mealplan(self, day: date, entry_type: str, recipe_id: str) -> None:
        body = {"date": day.isoformat(), "entryType": entry_type, "recipeId": recipe_id}
        r = self.http.post("/api/households/mealplans", json=body)
        if r.status_code == 404:
            r = self.http.post("/api/groups/mealplans", json=body)
        r.raise_for_status()

    def delete_mealplan(self, entry_id: str) -> None:
        r = self.http.delete(f"/api/households/mealplans/{entry_id}")
        if r.status_code == 404:
            r = self.http.delete(f"/api/groups/mealplans/{entry_id}")
        r.raise_for_status()

    # ---- recipe lists, photos and the dinner rotation tag ----
    def recipes(self, search: str = "", tag_id: str | None = None) -> list[dict]:
        params = {"perPage": -1, "page": 1, "orderBy": "name", "orderDirection": "asc"}
        if search:
            params["search"] = search
        if tag_id:
            params["tags"] = tag_id
        data = self._get("/api/recipes", **params)
        items = data if isinstance(data, list) else data.get("items", [])
        return [{"id": r["id"], "slug": r["slug"], "name": r["name"],
                 "tags": [t.get("slug") for t in r.get("tags") or []]} for r in items]

    def tag(self, name: str, create: bool = False) -> dict | None:
        slug = "-".join(name.lower().split())
        r = self.http.get(f"/api/organizers/tags/slug/{slug}")
        if r.status_code == 200:
            return r.json()
        if not create:
            return None
        r = self.http.post("/api/organizers/tags", json={"name": name})
        r.raise_for_status()
        return r.json()

    def set_recipe_tag(self, slug: str, tag: dict, on: bool) -> None:
        rec = self._get(f"/api/recipes/{slug}")
        tags = [t for t in rec.get("tags") or [] if t.get("id") != tag["id"]]
        if on:
            tags.append({"id": tag["id"], "name": tag["name"], "slug": tag["slug"]})
        r = self.http.patch(f"/api/recipes/{slug}", json={"tags": tags})
        r.raise_for_status()

    def recipe_image(self, recipe_id: str) -> bytes | None:
        for name in ("min-original.webp", "original.webp"):
            r = self.http.get(f"/api/media/recipes/{recipe_id}/images/{name}")
            if r.status_code == 200 and r.content:
                return r.content
        return None

    # ---- recipes ----
    def recipe(self, slug_or_id: str) -> Recipe:
        d = self._get(f"/api/recipes/{slug_or_id}")
        ings = []
        for i in d.get("recipeIngredient", []):
            if i.get("title") and not (i.get("note") or i.get("food") or i.get("display")):
                continue  # section header
            food = (i.get("food") or {}).get("name")
            unit = (i.get("unit") or {}).get("name")
            qty = i.get("quantity") or None
            text = i.get("display") or i.get("originalText") or i.get("note") or ""
            ings.append(Ingredient(text=text.strip(), qty=qty if food else None, unit=unit if food else None, food=food))
        servings = d.get("recipeServings") or d.get("recipeYieldQuantity") or None
        return Recipe(
            id=d["id"], slug=d["slug"], name=d["name"],
            servings=float(servings) if servings else None,
            yield_text=d.get("recipeYield") or "",
            ingredients=ings,
            last_made=d.get("lastMade"),
            rating=d.get("rating"),
        )

    # ---- links ----
    def group_slug(self) -> str:
        if not getattr(self, "_group_slug", None):
            try:
                self._group_slug = self._get("/api/groups/self").get("slug") or "home"
            except httpx.HTTPError:
                self._group_slug = "home"
        return self._group_slug

    def recipe_url(self, public_url: str, slug: str) -> str:
        return f"{public_url}/g/{self.group_slug()}/r/{slug}"

    # ---- foods marked "on hand" in Mealie ----
    def household_slug(self) -> str | None:
        if self._household_slug is None:
            try:
                self._household_slug = self._get("/api/households/self").get("slug")
            except httpx.HTTPError:
                self._household_slug = ""
        return self._household_slug or None

    def on_hand_foods(self) -> set[str]:
        try:
            data = self._get("/api/foods", perPage=-1, page=1)
        except httpx.HTTPError:
            return set()
        slug = self.household_slug()
        names = set()
        for f in data.get("items", []):
            households = f.get("householdsWithIngredientFood") or []
            if f.get("onHand") or (slug and slug in households):
                names.add(f["name"].strip().lower())
        return names

    # ---- food cleanup ----
    def all_foods(self) -> list[dict]:
        data = self._get("/api/foods", perPage=-1, page=1)
        return [{"id": f["id"], "name": f["name"], "aliases": [a.get("name") for a in (f.get("aliases") or [])]}
                for f in data.get("items", [])]

    def merge_foods(self, from_id: str, to_id: str) -> None:
        """Mealie moves recipe ingredients onto to_id and deletes from_id."""
        r = self.http.put("/api/foods/merge", json={"fromFood": from_id, "toFood": to_id})
        r.raise_for_status()

    def add_aliases(self, food_id: str, names: list[str]) -> None:
        """Keep merged names as aliases so Mealie's parser still recognizes them."""
        food = self._get(f"/api/foods/{food_id}")
        have = {(a.get("name") or "").lower() for a in food.get("aliases") or []} | {food["name"].lower()}
        new = [n for n in names if n.lower() not in have]
        if not new:
            return
        food["aliases"] = (food.get("aliases") or []) + [{"name": n} for n in new]
        r = self.http.put(f"/api/foods/{food_id}", json=food)
        r.raise_for_status()

    def backup(self) -> bool:
        """Ask Mealie to make a backup (needs an admin token). False if it couldn't."""
        try:
            r = self.http.post("/api/admin/backups", timeout=300)
            return r.status_code < 300
        except httpx.HTTPError:
            return False
