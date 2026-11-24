from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from app import groceries, purchases, recipes, tasks
from app.db import connect

AUTHOR = "Claude"

INSTRUCTIONS = """\
frigo tient la liste d'épicerie, les tâches et les gros achats de la maison de Jacob et
Catherine.

Épicerie :
- Pour une recette, ajouter tous les ingrédients en un seul appel à add_groceries, avec le nom
  de la recette dans `tag`.
- Dans ce même appel, joindre `recipe` : frigo en garde une fiche, consultable dans l'app. Y
  mettre le lien d'origine, tous les ingrédients tels qu'écrits (préparation et produits en
  stock compris) et les étapes reformulées brièvement, sans recopier le texte du site.
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

Tâches (un tableau À faire, En cours, Fait) :
- Avant d'ajouter, consulter list_tasks : si la tâche y est déjà, la modifier plutôt que d'en
  créer une deuxième.
- Ne confier une tâche à quelqu'un que si l'utilisateur le dit, avec un prénom de `people`.
  Le jeton est partagé : on ne sait pas qui parle, alors « pour moi » demande de préciser le
  prénom.
- Une échéance relative (« vendredi », « dans deux semaines ») se calcule à partir de `today`
  de list_tasks. Priorité `normal`, sauf si c'est urgent ou demandé.
- Pour avancer, ramener ou réassigner une tâche, update_task.
- Les identifiants à passer aux autres outils viennent de list_tasks.

Gros achats (Idée, À l'étude, Prêt à acheter, Acheté) :
- Avant d'ajouter, consulter list_purchases : si l'achat y est déjà, le modifier plutôt que
  d'en créer un deuxième. Reprendre une étiquette de `tags` (« music room ») plutôt que d'en
  inventer une variante.
- Statut `idea`, sauf si c'est déjà à l'étude ou décidé. Priorité `normal`, sauf si c'est
  urgent ou demandé.
- Montants en dollars entiers : « autour de 1 200 $ » donne `budget` 1200.
- « On l'a payé 480 $ » : update_purchase avec `status` `bought` et `paid` 480.
- Modèles comparés, liens et notes vont dans `details`. update_purchase remplace tout le
  texte : pour ajouter une note, repartir des détails renvoyés par list_purchases.
- Les identifiants à passer aux autres outils viennent de list_purchases.
"""

Status = Literal["todo", "doing", "done"]
Priority = Literal["low", "normal", "high"]
PurchaseStatus = Literal["idea", "researching", "ready", "bought"]


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


class RecipeIngredient(BaseModel):
    name: str = Field(description="Tel qu'écrit dans la recette, ex. « oignons émincés »")
    quantity: str = Field("", description="Ex. « 2 », « 1 boîte (796 ml) »")


class NewRecipe(BaseModel):
    url: str = Field("", description="Lien de la recette d'origine (http ou https)")
    servings: str = Field("", description="Ex. « 6 portions »")
    time: str = Field("", description="Temps total, ex. « 1 h 30 »")
    ingredients: list[RecipeIngredient] = Field(
        description="Tous les ingrédients, y compris ceux toujours en stock"
    )
    steps: list[str] = Field(description="Étapes reformulées en peu de mots, une par élément")


class GroceryList(BaseModel):
    to_buy: list[Grocery]
    bought: list[Grocery]
    staples: list[str] = Field(description="Toujours en stock : jamais ajoutés par add_groceries")
    aisles: list[str] = Field(description="Rayons, dans l'ordre où on parcourt l'épicerie")


class AddResult(BaseModel):
    added: list[AddedGrocery]
    skipped_staples: list[str] = Field(description="Laissés de côté : toujours en stock")
    recipe: str = Field("", description="Nom de la fiche de recette enregistrée, s'il y a lieu")


class IdsResult(BaseModel):
    done: list[int]
    skipped: list[int] = Field(description="Introuvables, ou déjà dans l'état demandé")


class TaskOut(BaseModel):
    id: int
    title: str
    note: str
    status: Status
    priority: Priority
    assignee: str = Field(description="Prénom, vide si la tâche n'est à personne")
    due: str = Field(description="Échéance AAAA-MM-JJ, vide s'il n'y en a pas")
    done_on: str = Field(description="Date de fin AAAA-MM-JJ, pour les tâches faites")
    added_by: str


class TaskList(BaseModel):
    tasks: list[TaskOut]
    today: str = Field(description="Date du jour AAAA-MM-JJ, pour calculer une échéance")
    people: list[str] = Field(description="Prénoms à qui on peut confier une tâche")


class NewTask(BaseModel):
    title: str = Field(description="Ex. « Appeler le plombier »")
    note: str = ""
    status: Status = "todo"
    priority: Priority = "normal"
    assignee: str = Field("", description="Prénom parmi `people` de list_tasks ; vide : personne")
    due: str = Field("", description="Échéance AAAA-MM-JJ ; vide : aucune")


class PurchaseOut(BaseModel):
    id: int
    title: str
    details: str
    status: PurchaseStatus
    priority: Priority
    budget: int | None = Field(description="Budget estimé en dollars")
    paid: int | None = Field(description="Prix payé en dollars")
    tags: list[str]
    bought_on: str = Field(description="Date d'achat AAAA-MM-JJ, pour les achats faits")
    added_by: str


class PurchaseList(BaseModel):
    purchases: list[PurchaseOut]
    totals: dict[str, int] = Field(
        description="Somme des budgets par statut en cours, en dollars, selon le filtre `tag`"
    )
    tags: list[str] = Field(description="Étiquettes déjà utilisées par les gros achats")


class NewPurchase(BaseModel):
    title: str = Field(description="Ex. « Système de son »")
    details: str = Field("", description="Modèles comparés, liens, notes")
    status: PurchaseStatus = "idea"
    priority: Priority = "normal"
    budget: int | None = Field(None, description="Budget estimé en dollars, ex. 1200")
    paid: int | None = Field(None, description="Prix payé en dollars, une fois acheté")
    tags: list[str] = Field(
        default_factory=list, description="La pièce ou le projet, ex. « music room »"
    )


def _purchase_out(purchase: purchases.Purchase) -> PurchaseOut:
    return PurchaseOut(
        id=purchase.id,
        title=purchase.title,
        details=purchase.details,
        status=purchase.status,
        priority=tasks.PRIORITIES[purchase.priority],
        budget=purchase.budget,
        paid=purchase.paid,
        tags=purchase.tags,
        bought_on=purchase.bought_on.isoformat() if purchase.bought_on else "",
        added_by=purchase.added_by,
    )


def _task_out(task: tasks.Task) -> TaskOut:
    return TaskOut(
        id=task.id,
        title=task.title,
        note=task.note,
        status=task.status,
        priority=tasks.PRIORITIES[task.priority],
        assignee=task.assignee,
        due=task.due.isoformat() if task.due else "",
        done_on=task.done_on.isoformat() if task.done_on and task.status == "done" else "",
        added_by=task.added_by,
    )


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


def build_mcp(db_path: Path, people: list[str]) -> MCPServer:
    """`people` : les prénoms à qui on peut confier une tâche."""
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
        items: list[NewGrocery],
        tag: str = "",
        include_staples: bool = False,
        recipe: NewRecipe | None = None,
    ) -> AddResult:
        """Ajoute des articles à l'épicerie. `tag` s'applique à tous, ex. le nom d'une recette.

        Les produits toujours en stock sont laissés de côté. `include_staples` les ajoute quand
        même : seulement si l'utilisateur demande lui-même ce produit (« il n'y a plus de
        farine »), jamais pour une recette.

        `recipe` enregistre la fiche de la recette sous le nom de `tag`, ou remplace celle qui
        porte déjà ce nom.
        """
        if recipe and not tag.strip():
            raise ToolError("`tag` est obligatoire avec `recipe` : c'est le nom de la recette.")
        with db() as con:
            try:
                # Vérifié avant d'ajouter quoi que ce soit, pour ne pas laisser un ajout à moitié.
                if recipe:
                    recipes.check_url(recipe.url)
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
            saved = (
                recipes.save(
                    con,
                    tag,
                    url=recipe.url,
                    servings=recipe.servings,
                    time=recipe.time,
                    ingredients=[
                        recipes.Ingredient(i.quantity, i.name) for i in recipe.ingredients
                    ],
                    steps=recipe.steps,
                    added_by=AUTHOR,
                )
                if recipe
                else None
            )
            return AddResult(
                added=[
                    AddedGrocery(
                        **_out(groceries.get_item(con, a.id)).model_dump(), merged=a.merged
                    )
                    for a in groceries.add_items(con, parsed, AUTHOR)
                ],
                skipped_staples=[item.name for item in skipped],
                recipe=saved.name if saved else "",
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

    @mcp.tool()
    def list_tasks(
        status: Status | None = None, assignee: str = "", include_done: bool = False
    ) -> TaskList:
        """Liste les tâches à faire et en cours ; les faites seulement avec `include_done` ou
        `status` « done », et seulement celles des 14 derniers jours.

        `assignee` filtre par prénom ; « - » ne garde que les tâches qui ne sont à personne.
        """
        try:
            person = None
            if assignee == "-":
                person = ""
            elif assignee.strip():
                person = tasks.find_person(assignee, people)
        except ValueError as error:
            raise ToolError(str(error)) from error
        with db() as con:
            found = tasks.list_tasks(con, status, person)
        if not (include_done or status == "done"):
            found = [t for t in found if t.status != "done"]
        return TaskList(
            tasks=[_task_out(t) for t in found], today=tasks.today().isoformat(), people=people
        )

    @mcp.tool()
    def add_tasks(items: list[NewTask]) -> list[TaskOut]:
        """Ajoute des tâches. Si l'une est invalide, aucune n'est ajoutée."""
        with db() as con:
            try:
                ids = tasks.add(
                    con,
                    [
                        tasks.Fields(
                            title=t.title,
                            note=t.note,
                            status=t.status,
                            priority=tasks.PRIORITIES.index(t.priority),
                            assignee=t.assignee,
                            due=tasks.parse_due(t.due),
                        )
                        for t in items
                    ],
                    AUTHOR,
                    people,
                )
            except ValueError as error:
                raise ToolError(str(error)) from error
            return [_task_out(tasks.get(con, i)) for i in ids]

    @mcp.tool()
    def update_task(
        id: int,
        title: str | None = None,
        note: str | None = None,
        status: Status | None = None,
        priority: Priority | None = None,
        assignee: str | None = None,
        due: str | None = None,
    ) -> TaskOut:
        """Modifie une tâche. Seuls les champs fournis changent ; `assignee` ou `due` vide
        retire la personne ou l'échéance."""
        changes = {
            "title": title,
            "note": note,
            "status": status,
            "priority": tasks.PRIORITIES.index(priority) if priority else None,
            "assignee": assignee,
        }
        with db() as con:
            task = tasks.get(con, id)
            if task is None:
                raise ToolError(f"Aucune tâche {id}.")
            try:
                fields = replace(task, **{k: v for k, v in changes.items() if v is not None})
                if due is not None:
                    fields.due = tasks.parse_due(due)
                return _task_out(tasks.update(con, id, fields, people))
            except (LookupError, ValueError) as error:
                raise ToolError(str(error)) from error

    @mcp.tool()
    def remove_tasks(ids: list[int]) -> IdsResult:
        """Supprime des tâches, faites ou non."""
        with db() as con:
            done = [i for i in ids if tasks.remove(con, i)]
        return IdsResult(done=done, skipped=[i for i in ids if i not in done])

    @mcp.tool()
    def list_purchases(
        status: PurchaseStatus | None = None, tag: str = "", include_bought: bool = False
    ) -> PurchaseList:
        """Liste les gros achats en cours ; les achetés seulement avec `include_bought` ou
        `status` « bought ». `tag` filtre par étiquette."""
        with db() as con:
            found = purchases.list_purchases(con, tag)
            tags = purchases.tag_names(con)
        totals = purchases.totals(found)
        if status:
            found = [p for p in found if p.status == status]
        elif not include_bought:
            found = [p for p in found if not p.bought]
        return PurchaseList(purchases=[_purchase_out(p) for p in found], totals=totals, tags=tags)

    @mcp.tool()
    def add_purchases(items: list[NewPurchase]) -> list[PurchaseOut]:
        """Ajoute des gros achats. Si l'un est invalide, aucun n'est ajouté."""
        with db() as con:
            try:
                ids = purchases.add(
                    con,
                    [
                        purchases.Fields(
                            title=p.title,
                            details=p.details,
                            status=p.status,
                            priority=tasks.PRIORITIES.index(p.priority),
                            budget=p.budget or None,
                            paid=p.paid or None,
                            tags=p.tags,
                        )
                        for p in items
                    ],
                    AUTHOR,
                )
            except ValueError as error:
                raise ToolError(str(error)) from error
            return [_purchase_out(purchases.get(con, i)) for i in ids]

    @mcp.tool()
    def update_purchase(
        id: int,
        title: str | None = None,
        details: str | None = None,
        status: PurchaseStatus | None = None,
        priority: Priority | None = None,
        budget: int | None = None,
        paid: int | None = None,
        tags: list[str] | None = None,
    ) -> PurchaseOut:
        """Modifie un gros achat. Seuls les champs fournis changent ; `budget` ou `paid` à 0
        efface le montant, `tags` remplace toutes les étiquettes."""
        changes = {
            "title": title,
            "details": details,
            "status": status,
            "priority": tasks.PRIORITIES.index(priority) if priority else None,
            "tags": tags,
        }
        with db() as con:
            purchase = purchases.get(con, id)
            if purchase is None:
                raise ToolError(f"Aucun gros achat {id}.")
            fields = replace(purchase, **{k: v for k, v in changes.items() if v is not None})
            if budget is not None:
                fields.budget = budget or None
            if paid is not None:
                fields.paid = paid or None
            try:
                return _purchase_out(purchases.update(con, id, fields))
            except (LookupError, ValueError) as error:
                raise ToolError(str(error)) from error

    @mcp.tool()
    def remove_purchases(ids: list[int]) -> IdsResult:
        """Supprime des gros achats, achetés ou non."""
        with db() as con:
            done = [i for i in ids if purchases.remove(con, i)]
        return IdsResult(done=done, skipped=[i for i in ids if i not in done])

    return mcp
