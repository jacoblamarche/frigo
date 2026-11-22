import json
import sqlite3
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.db import bump_version, now
from app.groceries import display, normalize


@dataclass
class Ingredient:
    quantity: str
    name: str


@dataclass
class Recipe:
    id: int
    name: str
    url: str
    servings: str
    time: str
    ingredients: list[Ingredient]
    steps: list[str]
    added_by: str

    @property
    def site(self) -> str:
        return urlsplit(self.url).hostname.removeprefix("www.") if self.url else ""


def check_url(url: str) -> str:
    # Affiché comme lien : un « javascript: » s'exécuterait au clic.
    url = url.strip()
    if url and (urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname):
        raise ValueError(f"Lien invalide : « {url} ». Il doit commencer par http:// ou https://.")
    return url


def _recipe(row: sqlite3.Row) -> Recipe:
    return Recipe(
        id=row["id"],
        name=row["name"],
        url=row["url"],
        servings=row["servings"],
        time=row["time"],
        ingredients=[Ingredient(**i) for i in json.loads(row["ingredients"])],
        steps=json.loads(row["steps"]),
        added_by=row["added_by"],
    )


def save(
    con: sqlite3.Connection,
    name: str,
    *,
    url: str = "",
    servings: str = "",
    time: str = "",
    ingredients: list[Ingredient],
    steps: list[str],
    added_by: str,
) -> Recipe:
    """Crée la fiche, ou remplace celle qui porte déjà ce nom."""
    if not name.strip():
        raise ValueError("Une recette doit avoir un nom.")
    url = check_url(url)
    ingredients_json = json.dumps(
        [
            {"quantity": i.quantity.strip(), "name": i.name.strip()}
            for i in ingredients
            if i.name.strip()
        ],
        ensure_ascii=False,
    )
    steps_json = json.dumps([s.strip() for s in steps if s.strip()], ensure_ascii=False)
    stamp = now()
    with con:
        con.execute(
            "INSERT INTO recipes (name, name_key, url, servings, time, ingredients, steps,"
            " added_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (name_key) DO UPDATE SET name = excluded.name, url = excluded.url,"
            " servings = excluded.servings, time = excluded.time,"
            " ingredients = excluded.ingredients, steps = excluded.steps,"
            " added_by = excluded.added_by, updated_at = excluded.updated_at",
            (
                display(name),
                normalize(name),
                url,
                servings.strip(),
                time.strip(),
                ingredients_json,
                steps_json,
                added_by,
                stamp,
                stamp,
            ),
        )
        bump_version(con)
    return by_name(con, name)


def list_recipes(con: sqlite3.Connection) -> list[Recipe]:
    """Les plus récentes d'abord : une recette importée de nouveau remonte en tête."""
    return [_recipe(row) for row in con.execute("SELECT * FROM recipes ORDER BY updated_at DESC")]


def get(con: sqlite3.Connection, recipe_id: int) -> Recipe | None:
    row = con.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    return _recipe(row) if row else None


def by_name(con: sqlite3.Connection, name: str) -> Recipe | None:
    row = con.execute("SELECT * FROM recipes WHERE name_key = ?", (normalize(name),)).fetchone()
    return _recipe(row) if row else None


def for_tags(con: sqlite3.Connection, tags: list[str]) -> list[Recipe]:
    return [recipe for tag in tags if (recipe := by_name(con, tag))]


def delete(con: sqlite3.Connection, recipe_id: int) -> bool:
    with con:
        deleted = con.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,)).rowcount
        if deleted:
            bump_version(con)
    return bool(deleted)
