import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.db import bump_version, now
from app.groceries import display, normalize

# L'image slim n'a pas de base de fuseaux horaires : le paquet tzdata la fournit.
TIMEZONE = ZoneInfo("America/Toronto")
STATUSES = ("todo", "doing", "done")
# Le rang est la valeur en base.
PRIORITIES = ("low", "normal", "high")
PRIORITY_LABELS = ("Basse", "Normale", "Haute")
NORMAL = 1
# Fait n'affiche que les tâches finies depuis ce nombre de jours ; les autres restent en base.
DONE_DAYS = 14
DAYS = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")
MONTHS = (
    "janv.", "févr.", "mars", "avr.", "mai", "juin",
    "juil.", "août", "sept.", "oct.", "nov.", "déc.",
)  # fmt: skip


@dataclass(frozen=True)
class Column:
    status: str
    slug: str
    label: str
    empty: str


COLUMNS = (
    Column("todo", "a-faire", "À faire", "Rien à faire."),
    Column("doing", "en-cours", "En cours", "Rien en cours."),
    Column("done", "fait", "Fait", f"Rien de fait ces {DONE_DAYS} derniers jours."),
)


@dataclass
class Fields:
    """Ce qu'on peut changer d'une tâche."""

    title: str
    note: str = ""
    status: str = "todo"
    priority: int = NORMAL
    assignee: str = ""
    due: date | None = None


@dataclass(kw_only=True)
class Task(Fields):
    id: int
    added_by: str
    done_on: date | None = None

    @property
    def priority_label(self) -> str:
        return PRIORITY_LABELS[self.priority]

    @property
    def next_column(self) -> Column | None:
        index = STATUSES.index(self.status) + 1
        return COLUMNS[index] if index < len(COLUMNS) else None

    @property
    def late(self) -> bool:
        return self.due is not None and self.status != "done" and self.due < today()


def today() -> date:
    return datetime.now(TIMEZONE).date()


def day_label(day: date, ref: date | None = None) -> str:
    """« aujourd'hui », « demain », « hier », sinon « jeu. 20 nov. »."""
    ref = ref or today()
    match (day - ref).days:
        case 0:
            return "aujourd'hui"
        case 1:
            return "demain"
        case -1:
            return "hier"
    label = f"{DAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1]}"
    return label if day.year == ref.year else f"{label} {day.year}"


def column_for(slug: str) -> Column:
    return next((c for c in COLUMNS if c.slug == slug), COLUMNS[0])


def parse_due(text: str) -> date | None:
    text = text.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"Échéance invalide : « {text} ». Format attendu : AAAA-MM-JJ.") from None


def find_person(name: str, people: list[str]) -> str:
    """Le prénom tel qu'enregistré, à la casse et aux accents près."""
    found = next((p for p in people if normalize(p) == normalize(name)), None)
    if found is None:
        raise ValueError(f"Personne inconnue : « {name} ». Choix : {', '.join(people)}.")
    return found


def check(fields: Fields, people: list[str]) -> Fields:
    """Nettoie les champs. `people` : les prénoms à qui on peut confier une tâche."""
    title = display(fields.title)
    if not title:
        raise ValueError("Une tâche doit avoir un titre.")
    if fields.status not in STATUSES:
        raise ValueError(f"Colonne inconnue : « {fields.status} ».")
    if fields.priority not in range(len(PRIORITIES)):
        raise ValueError(f"Priorité inconnue : « {fields.priority} ».")
    assignee = find_person(fields.assignee, people) if fields.assignee.strip() else ""
    return replace(fields, title=title, note=fields.note.strip(), assignee=assignee)


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day else None


def _task(row: sqlite3.Row) -> Task:
    done_at = row["done_at"]
    return Task(
        id=row["id"],
        title=row["title"],
        note=row["note"],
        status=row["status"],
        priority=row["priority"],
        assignee=row["assignee"],
        due=date.fromisoformat(row["due"]) if row["due"] else None,
        added_by=row["added_by"],
        done_on=datetime.fromisoformat(done_at).astimezone(TIMEZONE).date() if done_at else None,
    )


def add(
    con: sqlite3.Connection, items: list[Fields], added_by: str, people: list[str]
) -> list[int]:
    """Ajoute toutes les tâches, ou aucune si l'une est invalide."""
    stamp = now()
    with con:
        ids = []
        for fields in items:
            fields = check(fields, people)
            ids.append(
                con.execute(
                    "INSERT INTO tasks (title, note, status, priority, assignee, due, done_at,"
                    " added_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        fields.title,
                        fields.note,
                        fields.status,
                        fields.priority,
                        fields.assignee,
                        _iso(fields.due),
                        stamp if fields.status == "done" else None,
                        added_by,
                        stamp,
                        stamp,
                    ),
                ).lastrowid
            )
        if ids:
            bump_version(con)
    return ids


def update(con: sqlite3.Connection, task_id: int, fields: Fields, people: list[str]) -> Task:
    """Remplace les champs. En arrivant dans Fait, la tâche prend une date de fin ; en en
    sortant, elle la perd."""
    fields = check(fields, people)
    stamp = now()
    with con:
        row = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise LookupError(f"Aucune tâche {task_id}.")
        done_at = None
        if fields.status == "done":
            done_at = row["done_at"] if row["status"] == "done" else stamp
        con.execute(
            "UPDATE tasks SET title = ?, note = ?, status = ?, priority = ?, assignee = ?,"
            " due = ?, done_at = ?, updated_at = ? WHERE id = ?",
            (
                fields.title,
                fields.note,
                fields.status,
                fields.priority,
                fields.assignee,
                _iso(fields.due),
                done_at,
                stamp,
                task_id,
            ),
        )
        bump_version(con)
    return get(con, task_id)


def advance(con: sqlite3.Connection, task_id: int, from_status: str) -> bool:
    """Passe à la colonne suivante, seulement si la tâche est encore dans `from_status` : un
    double toucher, ou l'autre personne qui l'a déjà avancée, ne lui fait pas sauter une
    colonne."""
    if from_status not in STATUSES[:-1]:
        return False
    to = STATUSES[STATUSES.index(from_status) + 1]
    stamp = now()
    with con:
        moved = con.execute(
            "UPDATE tasks SET status = ?, done_at = ?, updated_at = ? WHERE id = ? AND status = ?",
            (to, stamp if to == "done" else None, stamp, task_id, from_status),
        ).rowcount
        if moved:
            bump_version(con)
    return bool(moved)


def remove(con: sqlite3.Connection, task_id: int) -> bool:
    with con:
        deleted = con.execute("DELETE FROM tasks WHERE id = ?", (task_id,)).rowcount
        if deleted:
            bump_version(con)
    return bool(deleted)


def get(con: sqlite3.Connection, task_id: int) -> Task | None:
    row = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _task(row) if row else None


def list_tasks(
    con: sqlite3.Connection, status: str | None = None, assignee: str | None = None
) -> list[Task]:
    """`assignee` : un prénom, « » pour les tâches à personne, None pour tout le monde.

    Par priorité, puis échéance, puis ancienneté ; dans Fait, la plus récente d'abord.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=DONE_DAYS)).isoformat(timespec="milliseconds")
    where, params = ["(status != 'done' OR done_at >= ?)"], [cutoff]
    if status:
        where.append("status = ?")
        params.append(status)
    if assignee is not None:
        where.append("assignee = ?")
        params.append(assignee)
    rows = con.execute(
        f"SELECT * FROM tasks WHERE {' AND '.join(where)}"
        " ORDER BY CASE status WHEN 'todo' THEN 0 WHEN 'doing' THEN 1 ELSE 2 END,"
        " done_at DESC, priority DESC, due IS NULL, due, created_at, id",
        params,
    ).fetchall()
    return [_task(row) for row in rows]


def board(con: sqlite3.Connection, assignee: str | None = None) -> dict[str, list[Task]]:
    tasks = list_tasks(con, assignee=assignee)
    return {status: [t for t in tasks if t.status == status] for status in STATUSES}
