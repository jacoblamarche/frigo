from contextlib import closing
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from app import groceries
from app.db import connect

AUTHOR = "Claude"

INSTRUCTIONS = """\
frigo tient la liste d'épicerie de Jacob et Catherine.

- Pour une recette, ajouter tous les ingrédients en un seul appel à add_groceries, avec le nom
  de la recette dans `tag`.
- Nommer l'article comme on le cherche à l'épicerie, sans la préparation : « oignons
  émincés » devient « Oignons ». La quantité reste telle qu'écrite (« 2 tasses », « 400 g »).
- Avant d'ajouter, consulter list_groceries : si un article équivalent y est déjà (« Oignons »
  pour « oignons jaunes »), reprendre son nom exact pour que les deux fusionnent.
- Un article déjà à acheter n'est jamais dupliqué : la quantité s'ajoute (« 2 + 1 ») et les
  étiquettes se cumulent.
- Les identifiants à passer aux autres outils viennent de list_groceries.
"""


class Grocery(BaseModel):
    id: int
    name: str
    quantity: str
    tags: list[str]
    bought: bool
    added_by: str


class AddedGrocery(Grocery):
    merged: bool = Field(description="Vrai si l'article était déjà sur la liste")


class NewGrocery(BaseModel):
    name: str = Field(description="Nom de l'article, ex. « Oignons »")
    quantity: str = Field("", description="Quantité telle qu'écrite, ex. « 2 », « 400 g »")
    tags: list[str] = Field(default_factory=list, description="Étiquettes de cet article seul")


class GroceryList(BaseModel):
    to_buy: list[Grocery]
    bought: list[Grocery]


class IdsResult(BaseModel):
    done: list[int]
    skipped: list[int] = Field(description="Introuvables, ou déjà dans l'état demandé")


def _out(item: groceries.Item) -> Grocery:
    return Grocery(
        id=item.id,
        name=item.name,
        quantity=item.quantity,
        tags=item.tags,
        bought=item.checked,
        added_by=item.added_by,
    )


def build_mcp(db_path: Path) -> MCPServer:
    mcp = MCPServer(name="frigo", instructions=INSTRUCTIONS)

    def db():
        return closing(connect(db_path))

    @mcp.tool()
    def list_groceries(tag: str = "", include_bought: bool = False) -> GroceryList:
        """Liste ce qu'il reste à acheter et, sur demande, les articles déjà cochés.

        `tag` filtre par étiquette ; « - » ne garde que les articles sans étiquette.
        """
        with db() as con:
            return GroceryList(
                to_buy=[_out(i) for i in groceries.list_items(con, tag)],
                bought=[_out(i) for i in groceries.list_items(con, checked=True)]
                if include_bought
                else [],
            )

    @mcp.tool()
    def add_groceries(items: list[NewGrocery], tag: str = "") -> list[AddedGrocery]:
        """Ajoute des articles à l'épicerie. `tag` s'applique à tous, ex. le nom d'une recette."""
        parsed = [
            groceries.ParsedItem(
                groceries.display(i.name),
                i.quantity.strip(),
                [groceries.display(t) for t in [*i.tags, tag] if t.strip()],
            )
            for i in items
            if i.name.strip()
        ]
        with db() as con:
            return [
                AddedGrocery(**_out(groceries.get_item(con, a.id)).model_dump(), merged=a.merged)
                for a in groceries.add_items(con, parsed, AUTHOR)
            ]

    @mcp.tool()
    def update_grocery(
        id: int,
        name: str | None = None,
        quantity: str | None = None,
        tags: list[str] | None = None,
    ) -> Grocery:
        """Modifie un article. Seuls les champs fournis changent ; `tags` remplace toutes ses
        étiquettes."""
        with db() as con:
            try:
                return _out(groceries.update_item(con, id, name, quantity, tags))
            except (LookupError, ValueError) as error:
                raise ToolError(str(error)) from error

    @mcp.tool()
    def mark_bought(ids: list[int]) -> IdsResult:
        """Coche des articles comme achetés."""
        with db() as con:
            done = groceries.mark_bought(con, ids)
        return IdsResult(done=done, skipped=[i for i in ids if i not in done])

    @mcp.tool()
    def remove_groceries(ids: list[int]) -> IdsResult:
        """Retire des articles de la liste, achetés ou non."""
        with db() as con:
            done = [i for i in ids if groceries.remove(con, i)]
        return IdsResult(done=done, skipped=[i for i in ids if i not in done])

    @mcp.tool()
    def clear_bought() -> int:
        """Retire tous les articles cochés. Renvoie combien ont été retirés."""
        with db() as con:
            return groceries.clear_checked(con)

    return mcp
