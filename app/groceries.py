import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field

from app.db import bump_version, now

UNITS = {
    "g", "kg", "mg", "ml", "l", "lb", "lbs", "oz",
    "boîte", "boîtes", "boite", "boites", "bouteille", "bouteilles", "conserve", "conserves",
    "douzaine", "douzaines", "gousse", "gousses", "paquet", "paquets", "pot", "pots",
    "sac", "sacs", "tasse", "tasses", "tranche", "tranches",
}  # fmt: skip
NUMBER = r"\d+(?:[.,]\d+)?|\d+/\d+|[½¼¾]"
NUMBER_RE = re.compile(rf"^(?:{NUMBER})$")
GLUED_RE = re.compile(rf"^({NUMBER})(g|kg|mg|ml|l|lb|lbs|oz)$", re.IGNORECASE)
BULLET_RE = re.compile(r"^\s*(?:[-*•–]|\d+[.)])\s+")


@dataclass
class ParsedItem:
    name: str
    quantity: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class Item:
    id: int
    name: str
    quantity: str
    checked: bool
    added_by: str
    tags: list[str]


def normalize(text: str) -> str:
    text = text.lower().replace("œ", "oe").replace("æ", "ae")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.split())


def display(text: str) -> str:
    text = " ".join(text.split())
    return text[:1].upper() + text[1:]


def _quantity_token(token: str) -> str | None:
    if NUMBER_RE.match(token):
        return token
    glued = GLUED_RE.match(token)
    return f"{glued[1]} {glued[2]}" if glued else None


def parse_line(line: str) -> ParsedItem | None:
    """« lait 2 L », « 3 oignons #lasagne », « 1 lb bœuf haché », « mozzarella 400g »."""
    line = BULLET_RE.sub("", line).strip().rstrip(",;")
    tags = [t[1:].replace("_", " ") for t in line.split() if t.startswith("#") and len(t) > 1]
    words = [w for w in line.split() if not w.startswith("#")]

    quantity = ""
    if len(words) > 1 and (q := _quantity_token(words[0])):
        quantity, words = q, words[1:]
        if len(words) > 1 and words[0].lower() in UNITS:
            quantity, words = f"{quantity} {words[0]}", words[1:]
    elif len(words) > 1 and (q := _quantity_token(words[-1])):
        quantity, words = q, words[:-1]
    elif len(words) > 2 and words[-1].lower() in UNITS and NUMBER_RE.match(words[-2]):
        quantity, words = f"{words[-2]} {words[-1]}", words[:-2]

    name = " ".join(words)
    if not name:
        return None
    return ParsedItem(display(name), quantity, [display(t) for t in tags])


def parse_text(text: str, tag: str = "") -> list[ParsedItem]:
    items = [item for line in text.splitlines() if (item := parse_line(line))]
    if tag.strip():
        for item in items:
            item.tags.append(display(tag))
    return items


def combine_quantities(first: str, second: str) -> str:
    if not first or not second:
        return first or second
    return f"{first} + {second}"


def _tag_id(con: sqlite3.Connection, name: str) -> int:
    con.execute(
        "INSERT INTO tags (name, name_key) VALUES (?, ?) ON CONFLICT (name_key) DO NOTHING",
        (name, normalize(name)),
    )
    return con.execute("SELECT id FROM tags WHERE name_key = ?", (normalize(name),)).fetchone()[0]


def _add_tags(con: sqlite3.Connection, item_id: int, tags: list[str]) -> None:
    for tag in tags:
        con.execute(
            "INSERT OR IGNORE INTO grocery_item_tags (item_id, tag_id) VALUES (?, ?)",
            (item_id, _tag_id(con, tag)),
        )


def _open_item(con: sqlite3.Connection, name_key: str) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM grocery_items WHERE name_key = ? AND checked = 0", (name_key,)
    ).fetchone()


def add_items(con: sqlite3.Connection, items: list[ParsedItem], added_by: str) -> list[int]:
    """Ajoute les articles ; un article déjà à acheter cumule quantité et étiquettes."""
    ids = []
    with con:
        for item in items:
            key = normalize(item.name)
            existing = _open_item(con, key)
            if existing:
                item_id = existing["id"]
                con.execute(
                    "UPDATE grocery_items SET quantity = ? WHERE id = ?",
                    (combine_quantities(existing["quantity"], item.quantity), item_id),
                )
            else:
                item_id = con.execute(
                    "INSERT INTO grocery_items (name, name_key, quantity, added_by, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (item.name, key, item.quantity, added_by, now()),
                ).lastrowid
            _add_tags(con, item_id, item.tags)
            con.execute(
                "INSERT INTO grocery_history (name_key, name, times, last_added_at)"
                " VALUES (?, ?, 1, ?) ON CONFLICT (name_key)"
                " DO UPDATE SET name = excluded.name, times = times + 1,"
                " last_added_at = excluded.last_added_at",
                (key, item.name, now()),
            )
            ids.append(item_id)
        if items:
            bump_version(con)
    return ids


def toggle(con: sqlite3.Connection, item_id: int) -> bool:
    """Coche ou décoche. Décocher un article déjà de retour sur la liste le fusionne avec lui."""
    with con:
        row = con.execute("SELECT * FROM grocery_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            return False
        if not row["checked"]:
            con.execute(
                "UPDATE grocery_items SET checked = 1, checked_at = ? WHERE id = ?",
                (now(), item_id),
            )
        elif existing := _open_item(con, row["name_key"]):
            con.execute(
                "UPDATE grocery_items SET quantity = ? WHERE id = ?",
                (combine_quantities(existing["quantity"], row["quantity"]), existing["id"]),
            )
            con.execute(
                "INSERT OR IGNORE INTO grocery_item_tags (item_id, tag_id)"
                " SELECT ?, tag_id FROM grocery_item_tags WHERE item_id = ?",
                (existing["id"], item_id),
            )
            con.execute("DELETE FROM grocery_items WHERE id = ?", (item_id,))
        else:
            con.execute(
                "UPDATE grocery_items SET checked = 0, checked_at = NULL WHERE id = ?", (item_id,)
            )
        bump_version(con)
    return True


def remove(con: sqlite3.Connection, item_id: int) -> bool:
    with con:
        deleted = con.execute("DELETE FROM grocery_items WHERE id = ?", (item_id,)).rowcount
        if deleted:
            bump_version(con)
    return bool(deleted)


def clear_checked(con: sqlite3.Connection) -> int:
    with con:
        deleted = con.execute("DELETE FROM grocery_items WHERE checked = 1").rowcount
        if deleted:
            bump_version(con)
    return deleted


def list_items(con: sqlite3.Connection, tag: str = "", checked: bool = False) -> list[Item]:
    """`tag` filtre par étiquette ; « - » ne garde que les articles sans étiquette."""
    rows = con.execute(
        "SELECT i.*, group_concat(t.name, char(31)) AS tag_names"
        " FROM grocery_items i"
        " LEFT JOIN grocery_item_tags it ON it.item_id = i.id"
        " LEFT JOIN tags t ON t.id = it.tag_id"
        " WHERE i.checked = ?"
        " GROUP BY i.id"
        + (" ORDER BY i.checked_at DESC, i.id DESC" if checked else " ORDER BY i.created_at, i.id"),
        (int(checked),),
    ).fetchall()
    items = [
        Item(
            id=row["id"],
            name=row["name"],
            quantity=row["quantity"],
            checked=bool(row["checked"]),
            added_by=row["added_by"],
            tags=sorted(row["tag_names"].split("\x1f")) if row["tag_names"] else [],
        )
        for row in rows
    ]
    if tag == "-":
        return [item for item in items if not item.tags]
    if tag:
        return [item for item in items if normalize(tag) in map(normalize, item.tags)]
    return items


def tag_counts(con: sqlite3.Connection) -> list[tuple[str, int]]:
    return [
        (row["name"], row["n"])
        for row in con.execute(
            "SELECT t.name, count(*) AS n FROM tags t"
            " JOIN grocery_item_tags it ON it.tag_id = t.id"
            " JOIN grocery_items i ON i.id = it.item_id AND i.checked = 0"
            " GROUP BY t.id ORDER BY t.name"
        )
    ]


def frequent(con: sqlite3.Connection, limit: int = 6) -> list[str]:
    """Articles souvent ajoutés qui ne sont pas déjà sur la liste."""
    return [
        row["name"]
        for row in con.execute(
            "SELECT h.name FROM grocery_history h"
            " WHERE h.times > 1 AND h.name_key NOT IN"
            " (SELECT name_key FROM grocery_items WHERE checked = 0)"
            " ORDER BY h.times DESC, h.last_added_at DESC LIMIT ?",
            (limit,),
        )
    ]
