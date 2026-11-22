import sqlite3
from datetime import UTC, datetime
from pathlib import Path

# Une entrée par version du schéma, jamais modifiée une fois publiée : PRAGMA user_version
# retient combien ont été appliquées.
MIGRATIONS = [
    """
    CREATE TABLE grocery_items (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        name_key TEXT NOT NULL,
        quantity TEXT NOT NULL DEFAULT '',
        checked INTEGER NOT NULL DEFAULT 0,
        checked_at TEXT,
        added_by TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    -- Un seul article à acheter par nom : un ajout en double fusionne au lieu de dupliquer.
    CREATE UNIQUE INDEX grocery_items_open_name ON grocery_items (name_key) WHERE checked = 0;

    CREATE TABLE tags (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        name_key TEXT NOT NULL UNIQUE
    );

    CREATE TABLE grocery_item_tags (
        item_id INTEGER NOT NULL REFERENCES grocery_items (id) ON DELETE CASCADE,
        tag_id INTEGER NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
        PRIMARY KEY (item_id, tag_id)
    );

    CREATE TABLE grocery_history (
        name_key TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        times INTEGER NOT NULL,
        last_added_at TEXT NOT NULL
    );

    CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
    INSERT INTO meta (key, value) VALUES ('version', 0);
    """,
    """
    CREATE TABLE staples (
        name_key TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        added_by TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """,
    # aisle_id NULL : article pas encore classé, affiché dans « Autre ». L'historique retient
    # le rayon de chaque article. Un ordre propre à chaque magasin s'ajoutera à côté de
    # `position`.
    """
    CREATE TABLE aisles (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        position INTEGER NOT NULL
    );
    INSERT INTO aisles (name, position) VALUES
        ('Fruits et légumes', 1),
        ('Boulangerie', 2),
        ('Viandes et poissons', 3),
        ('Produits laitiers et œufs', 4),
        ('Fromages', 5),
        ('Épicerie sèche', 6),
        ('Condiments et épices', 7),
        ('Collations', 8),
        ('Boissons', 9),
        ('Surgelés', 10),
        ('Hygiène et maison', 11),
        ('Autre', 12);
    ALTER TABLE grocery_items ADD COLUMN aisle_id INTEGER REFERENCES aisles (id);
    ALTER TABLE grocery_history ADD COLUMN aisle_id INTEGER REFERENCES aisles (id);
    """,
]


def connect(path: Path) -> sqlite3.Connection:
    # Une connexion par requête, mais FastAPI peut ouvrir la dépendance et exécuter la route
    # dans deux threads différents.
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def migrate(con: sqlite3.Connection) -> None:
    current = con.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(MIGRATIONS[current:], start=current + 1):
        con.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def bump_version(con: sqlite3.Connection) -> None:
    con.execute("UPDATE meta SET value = value + 1 WHERE key = 'version'")


def data_version(con: sqlite3.Connection) -> int:
    return con.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()[0]
