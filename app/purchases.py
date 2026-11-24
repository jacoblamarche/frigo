import re
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.db import bump_version, now
from app.groceries import display, normalize, tag_id
from app.tasks import NORMAL, PRIORITIES, PRIORITY_LABELS, TIMEZONE, today

BOUGHT = "bought"
# Sous les achats en cours, « Acheté récemment » remonte ce nombre de jours.
RECENT_DAYS = 30
# Au-delà, SQLite ne pourrait plus stocker l'entier.
MAX_AMOUNT = 10_000_000
# « 1 200 », « 1,200 » ou « 1200 », puis des cents facultatifs : « 479,99 ».
AMOUNT_RE = re.compile(r"(\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,](\d{1,2}))?")
LINK_RE = re.compile(r"https?://[^\s<>\"']+")


@dataclass(frozen=True)
class Status:
    value: str
    slug: str
    label: str


STATUSES = (
    Status("idea", "idee", "Idée"),
    Status("researching", "a-l-etude", "À l'étude"),
    Status("ready", "pret", "Prêt à acheter"),
    Status(BOUGHT, "achete", "Acheté"),
)
# Les totaux du haut, du plus proche de l'achat au plus lointain.
TOTALS = (("ready", "Prêt à acheter"), ("researching", "À l'étude"), ("idea", "Idées"))


def dollars(amount: int) -> str:
    """« 1 200 $ », avec des espaces insécables."""
    return f"{amount:,}".replace(",", " ") + " $"


@dataclass
class Fields:
    """Ce qu'on peut changer d'un gros achat. Montants en dollars entiers."""

    title: str
    details: str = ""
    status: str = "idea"
    priority: int = NORMAL
    budget: int | None = None
    paid: int | None = None
    tags: list[str] = field(default_factory=list)

    @property
    def links(self) -> list[tuple[str, str]]:
        """Les liens des détails, avec un libellé sans « https:// » ni « www. »."""
        found = (link.rstrip(".,;:!?)»") for link in LINK_RE.findall(self.details))
        return [
            (link, link.split("://", 1)[1].removeprefix("www.")) for link in dict.fromkeys(found)
        ]


@dataclass(kw_only=True)
class Purchase(Fields):
    id: int
    added_by: str
    bought_on: date | None = None

    @property
    def priority_label(self) -> str:
        return PRIORITY_LABELS[self.priority]

    @property
    def status_label(self) -> str:
        return status_named(self.status).label

    @property
    def bought(self) -> bool:
        return self.status == BOUGHT

    @property
    def recently_bought(self) -> bool:
        return self.bought_on is not None and self.bought_on > today() - timedelta(RECENT_DAYS)

    @property
    def gap(self) -> str:
        """L'écart entre le budget et le prix payé, vide s'il manque l'un des deux."""
        if self.budget is None or self.paid is None:
            return ""
        if self.paid < self.budget:
            return f"{dollars(self.budget - self.paid)} sous le budget"
        if self.paid > self.budget:
            return f"{dollars(self.paid - self.budget)} au-dessus du budget"
        return "pile dans le budget"


def status_named(value: str) -> Status:
    return next(s for s in STATUSES if s.value == value)


def status_for(slug: str) -> Status | None:
    return next((s for s in STATUSES if s.slug == slug), None)


def parse_amount(text: str) -> int | None:
    """« 1 200 $ », « ~1200 », « 479,99 » (arrondi à 480). Vide ou 0 : pas de montant."""
    cleaned = re.sub(r"[\s$~≈]", "", text)
    if not cleaned:
        return None
    match = AMOUNT_RE.fullmatch(cleaned)
    if match is None:
        raise ValueError(f"Montant invalide : « {text.strip()} ». Ex. : 1 200 $.")
    whole = re.sub(r"[.,]", "", match[1])
    return int(Decimal(f"{whole}.{match[2] or 0}").to_integral_value(ROUND_HALF_UP)) or None


def check(fields: Fields) -> Fields:
    title = display(fields.title)
    if not title:
        raise ValueError("Un gros achat doit avoir un titre.")
    if fields.status not in [s.value for s in STATUSES]:
        raise ValueError(f"Statut inconnu : « {fields.status} ».")
    if fields.priority not in range(len(PRIORITIES)):
        raise ValueError(f"Priorité inconnue : « {fields.priority} ».")
    for amount in (fields.budget, fields.paid):
        if amount is not None and not 0 < amount <= MAX_AMOUNT:
            raise ValueError(f"Montant hors limites : {amount} $.")
    # Les doublons (« Salon », « salon ») partagent la même étiquette en base.
    tags = [display(t) for t in fields.tags if t.strip()]
    return replace(fields, title=title, details=fields.details.strip(), tags=tags)


def _set_tags(con: sqlite3.Connection, purchase_id: int, tags: list[str]) -> None:
    con.execute("DELETE FROM purchase_tags WHERE purchase_id = ?", (purchase_id,))
    for tag in tags:
        con.execute(
            "INSERT OR IGNORE INTO purchase_tags (purchase_id, tag_id) VALUES (?, ?)",
            (purchase_id, tag_id(con, tag)),
        )


def _purchases(con: sqlite3.Connection, where: str = "1", params: tuple = ()) -> list[Purchase]:
    rows = con.execute(
        "SELECT p.*, group_concat(t.name, char(31)) AS tag_names FROM purchases p"
        " LEFT JOIN purchase_tags pt ON pt.purchase_id = p.id"
        " LEFT JOIN tags t ON t.id = pt.tag_id"
        f" WHERE {where} GROUP BY p.id"
        " ORDER BY p.status = 'bought', p.bought_at DESC, p.priority DESC,"
        " p.budget IS NULL, p.budget DESC, p.created_at, p.id",
        params,
    ).fetchall()
    return [
        Purchase(
            id=row["id"],
            title=row["title"],
            details=row["details"],
            status=row["status"],
            priority=row["priority"],
            budget=row["budget"],
            paid=row["paid"],
            tags=sorted(row["tag_names"].split("\x1f"), key=normalize) if row["tag_names"] else [],
            added_by=row["added_by"],
            bought_on=datetime.fromisoformat(row["bought_at"]).astimezone(TIMEZONE).date()
            if row["bought_at"]
            else None,
        )
        for row in rows
    ]


def add(con: sqlite3.Connection, items: list[Fields], added_by: str) -> list[int]:
    """Ajoute tous les achats, ou aucun si l'un est invalide."""
    stamp = now()
    with con:
        ids = []
        for fields in items:
            fields = check(fields)
            purchase_id = con.execute(
                "INSERT INTO purchases (title, details, status, priority, budget, paid,"
                " bought_at, added_by, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fields.title,
                    fields.details,
                    fields.status,
                    fields.priority,
                    fields.budget,
                    fields.paid,
                    stamp if fields.status == BOUGHT else None,
                    added_by,
                    stamp,
                    stamp,
                ),
            ).lastrowid
            _set_tags(con, purchase_id, fields.tags)
            ids.append(purchase_id)
        if ids:
            bump_version(con)
    return ids


def update(con: sqlite3.Connection, purchase_id: int, fields: Fields) -> Purchase:
    """Remplace les champs. En passant à Acheté, l'achat prend la date du jour ; en revenant en
    arrière, il la perd."""
    fields = check(fields)
    stamp = now()
    with con:
        row = con.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,)).fetchone()
        if row is None:
            raise LookupError(f"Aucun gros achat {purchase_id}.")
        bought_at = None
        if fields.status == BOUGHT:
            bought_at = row["bought_at"] if row["status"] == BOUGHT else stamp
        con.execute(
            "UPDATE purchases SET title = ?, details = ?, status = ?, priority = ?, budget = ?,"
            " paid = ?, bought_at = ?, updated_at = ? WHERE id = ?",
            (
                fields.title,
                fields.details,
                fields.status,
                fields.priority,
                fields.budget,
                fields.paid,
                bought_at,
                stamp,
                purchase_id,
            ),
        )
        _set_tags(con, purchase_id, fields.tags)
        bump_version(con)
    return get(con, purchase_id)


def remove(con: sqlite3.Connection, purchase_id: int) -> bool:
    with con:
        deleted = con.execute("DELETE FROM purchases WHERE id = ?", (purchase_id,)).rowcount
        if deleted:
            bump_version(con)
    return bool(deleted)


def get(con: sqlite3.Connection, purchase_id: int) -> Purchase | None:
    found = _purchases(con, "p.id = ?", (purchase_id,))
    return found[0] if found else None


def list_purchases(con: sqlite3.Connection, tag: str = "") -> list[Purchase]:
    """Tous les achats, ou ceux qui portent `tag`.

    Ceux en cours par priorité, puis budget, puis ancienneté ; les achetés à la fin, le plus
    récent d'abord.
    """
    found = _purchases(con)
    if tag:
        return [p for p in found if normalize(tag) in map(normalize, p.tags)]
    return found


def totals(purchases: list[Purchase]) -> dict[str, int]:
    """La somme des budgets par statut en cours ; un achat sans budget ne compte pas."""
    return {
        status: sum(p.budget or 0 for p in purchases if p.status == status) for status, _ in TOTALS
    }


def tag_names(con: sqlite3.Connection) -> list[str]:
    """Les étiquettes portées par au moins un gros achat."""
    return [
        row["name"]
        for row in con.execute(
            "SELECT DISTINCT t.name, t.name_key FROM tags t"
            " JOIN purchase_tags pt ON pt.tag_id = t.id ORDER BY t.name_key"
        )
    ]
