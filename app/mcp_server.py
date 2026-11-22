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
- Les produits de `staples` sont toujours en stock (achetés en vrac, par exemple) :
  add_groceries les laisse de côté et les renvoie dans `skipped_staples`, ce qu'il faut dire à
  l'utilisateur. Nommer l'ingrédient comme le produit en stock (« farine tout usage » →
  « Farine ») pour qu'il soit reconnu.
- Un article déjà à acheter n'est jamais dupliqué : la quantité s'ajoute (« 2 + 1 ») et les
  étiquettes se cumulent.
- Donner à chaque article son rayon (`aisle`), parmi les `aisles` de list_groceries : le
  gruyère va aux Fromages. Un article déjà classé garde le rayon qu'on lui a donné.
- Pour déplacer un article quand on le demande, update_grocery avec `aisle` : le choix est
  retenu pour la prochaine fois.
- Les identifiants à passer aux autres outils viennent de list_groceries.
"""


class Grocery(BaseModel):
    id: int
    name: str
    quantity: str
    tags: list[str]
    bought: bool
    added_by: str
    aisle: str


class AddedGrocery(Grocery):
    merged: bool = Field(description="Vrai si l'article était déjà sur la liste")


class NewGrocery(BaseModel):
    name: str = Field(description="Nom de l'article, ex. « Oignons »")
    quantity: str = Field("", description="Quantité telle qu'écrite, ex. « 2 », « 400 g »")
    tags: list[str] = Field(default_factory=list, description="Étiquettes de cet article seul")
    aisle: str = Field("", description="Rayon, parmi les `aisles` de list_groceries")


class GroceryList(BaseModel):
    to_buy: list[Grocery]
    bought: list[Grocery]
    staples: list[str] = Field(description="Toujours en stock : jamais ajoutés par add_groceries")
    aisles: list[str] = Field(description="Rayons, dans l'ordre où on parcourt l'épicerie")


class AddResult(BaseModel):
    added: list[AddedGrocery]
    skipped_staples: list[str] = Field(description="Laissés de côté : toujours en stock")


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
        aisle=item.aisle,
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
                staples=groceries.list_staples(con),
                aisles=[a.name for a in groceries.list_aisles(con)],
            )

    @mcp.tool()
    def add_groceries(
        items: list[NewGrocery], tag: str = "", include_staples: bool = False
    ) -> AddResult:
        """Ajoute des articles à l'épicerie. `tag` s'applique à tous, ex. le nom d'une recette.

        Les produits toujours en stock sont laissés de côté. `include_staples` les ajoute quand
        même : seulement si l'utilisateur demande lui-même ce produit (« il n'y a plus de
        farine »), jamais pour une recette.
        """
        with db() as con:
            try:
                parsed = [
                    groceries.ParsedItem(
                        groceries.display(i.name),
                        i.quantity.strip(),
                        [groceries.display(t) for t in [*i.tags, tag] if t.strip()],
                        groceries.find_aisle(con, i.aisle) if i.aisle.strip() else None,
                    )
                    for i in items
                    if i.name.strip()
                ]
            except ValueError as error:
                raise ToolError(str(error)) from error
            skipped = []
            if not include_staples:
                parsed, skipped = groceries.split_staples(con, parsed)
            return AddResult(
                added=[
                    AddedGrocery(
                        **_out(groceries.get_item(con, a.id)).model_dump(), merged=a.merged
                    )
                    for a in groceries.add_items(con, parsed, AUTHOR)
                ],
                skipped_staples=[item.name for item in skipped],
            )

    @mcp.tool()
    def update_grocery(
        id: int,
        name: str | None = None,
        quantity: str | None = None,
        tags: list[str] | None = None,
        aisle: str | None = None,
    ) -> Grocery:
        """Modifie un article. Seuls les champs fournis changent ; `tags` remplace toutes ses
        étiquettes. Le rayon choisi est retenu pour la prochaine fois."""
        with db() as con:
            try:
                aisle_id = groceries.find_aisle(con, aisle) if aisle and aisle.strip() else None
                return _out(groceries.update_item(con, id, name, quantity, tags, aisle_id))
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
