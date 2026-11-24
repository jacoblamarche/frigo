import hashlib
from collections.abc import Iterator
from contextlib import asynccontextmanager, closing
from dataclasses import replace
from functools import cache
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mcp.server.transport_security import TransportSecuritySettings

from app import groceries, purchases, recipes, tasks
from app.db import connect, data_version, migrate
from app.mcp_server import AUTHOR, build_mcp
from app.settings import Settings, bearer_ok, same_origin

APP_DIR = Path(__file__).parent
PUBLIC_PATHS = ("/healthz", "/static/")
MCP_PATH = "/mcp"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SECTIONS = [
    {"id": "epicerie", "label": "Épicerie", "path": "/epicerie"},
    {"id": "recettes", "label": "Recettes", "path": "/recettes"},
    {"id": "taches", "label": "Tâches", "path": "/taches"},
    {"id": "achats", "label": "Gros achats", "path": "/achats"},
]


@cache
def static_url(name: str) -> str:
    # Cloudflare et les navigateurs gardent /static/ en cache : l'empreinte du contenu change
    # l'URL à chaque version du fichier.
    digest = hashlib.sha256((APP_DIR / "static" / name).read_bytes()).hexdigest()[:10]
    return f"/static/{name}?v={digest}"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db_path = settings.data_dir / "frigo.sqlite3"
    with closing(connect(db_path)) as con:
        migrate(con)

    names = settings.names
    mcp = build_mcp(db_path, names)
    # Sans état et en JSON : chaque appel d'outil est une requête indépendante. La protection
    # contre le DNS rebinding n'accepterait que localhost ; le jeton protège déjà /mcp.
    mcp_routes = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    ).routes

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
    if settings.mcp_token:
        app.router.routes.extend(mcp_routes)
    templates = Jinja2Templates(directory=APP_DIR / "templates")
    templates.env.globals["mcp_author"] = AUTHOR
    templates.env.globals["static"] = static_url
    templates.env.filters["day"] = tasks.day_label
    templates.env.filters["dollars"] = purchases.dollars

    @app.middleware("http")
    async def identify(request: Request, call_next):
        if request.url.path.startswith(PUBLIC_PATHS):
            return await call_next(request)
        if request.url.path == MCP_PATH:
            if not bearer_ok(request.headers, settings.mcp_token):
                return PlainTextResponse(
                    "Jeton invalide.", status_code=401, headers={"WWW-Authenticate": "Bearer"}
                )
            return await call_next(request)
        client_host = request.client.host if request.client else ""
        person = settings.person_for(client_host, request.headers)
        if person is None:
            return PlainTextResponse("Accès refusé.", status_code=403)
        if request.method not in SAFE_METHODS and not same_origin(request.headers):
            return PlainTextResponse("Origine refusée.", status_code=403)
        request.state.person = person
        return await call_next(request)

    def get_db() -> Iterator:
        with closing(connect(db_path)) as con:
            yield con

    def render(request: Request, template: str, context: dict, status_code: int = 200) -> Response:
        return templates.TemplateResponse(
            request,
            template,
            {"person": request.state.person, "sections": SECTIONS, **context},
            status_code=status_code,
        )

    def unless_unchanged(request: Request, etag: str, build) -> Response:
        """304 tant que rien n'a changé depuis `etag`, sinon le fragment rendu par `build`."""
        headers = {"ETag": etag, "Cache-Control": "no-store"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        response = build()
        response.headers.update(headers)
        return response

    def with_query(path: str, **params: str) -> str:
        query = urlencode({k: v for k, v in params.items() if v})
        return f"{path}?{query}" if query else path

    def dated_etag(con) -> str:
        # La date compte : « en retard » et les fenêtres de Fait et d'Acheté récemment changent
        # à minuit.
        return f'"{data_version(con)}-{tasks.today()}"'

    def back(request: Request, url: str) -> Response:
        if request.headers.get("x-frigo-async"):
            return Response(status_code=204)
        return RedirectResponse(url, status_code=303)

    def grocery_context(con, tag: str) -> dict:
        items = groceries.list_items(con, tag)
        return {
            "section": "epicerie",
            "tag": tag,
            "recipe": recipes.by_name(con, tag) if tag and tag != "-" else None,
            "items": items,
            "by_aisle": groceries.by_aisle(items),
            "checked_items": groceries.list_items(con, checked=True),
            "tags": groceries.tag_counts(con),
            "total": len(groceries.list_items(con)),
            "untagged": len(groceries.list_items(con, "-")),
            "frequent": groceries.frequent(con),
            "version": data_version(con),
        }

    def groceries_url(tag: str) -> str:
        return f"/epicerie?{urlencode({'etiquette': tag})}" if tag else "/epicerie"

    def back_to_groceries(request: Request, tag: str) -> Response:
        return back(request, groceries_url(tag))

    def item_page(request: Request, con, item, tag: str, error: str = "") -> Response:
        return render(
            request,
            "article.html",
            {
                "section": "epicerie",
                "item": item,
                "recipes": recipes.for_tags(con, item.tags),
                "aisles": groceries.list_aisles(con),
                "tag": tag,
                "back": groceries_url(tag),
                "error": error,
            },
            status_code=409 if error else 200,
        )

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/")
    def home():
        return RedirectResponse("/epicerie")

    @app.get("/epicerie")
    def groceries_page(request: Request, etiquette: str = "", con=Depends(get_db)):
        return render(request, "epicerie.html", grocery_context(con, etiquette))

    @app.get("/epicerie/liste")
    def groceries_fragment(request: Request, etiquette: str = "", con=Depends(get_db)):
        return unless_unchanged(
            request,
            f'"{data_version(con)}"',
            lambda: render(request, "_epicerie_liste.html", grocery_context(con, etiquette)),
        )

    @app.post("/epicerie/ajouter")
    def add_groceries(
        request: Request,
        texte: str = Form(""),
        etiquette: str = Form(""),
        filtre: str = Form(""),
        con=Depends(get_db),
    ):
        groceries.add_items(con, groceries.parse_text(texte, etiquette), request.state.person)
        return back_to_groceries(request, filtre)

    @app.post("/epicerie/{item_id}/cocher")
    def toggle_grocery(request: Request, item_id: int, filtre: str = Form(""), con=Depends(get_db)):
        groceries.toggle(con, item_id)
        return back_to_groceries(request, filtre)

    @app.post("/epicerie/{item_id}/supprimer")
    def remove_grocery(request: Request, item_id: int, filtre: str = Form(""), con=Depends(get_db)):
        groceries.remove(con, item_id)
        return back_to_groceries(request, filtre)

    @app.post("/epicerie/vider")
    def clear_groceries(request: Request, filtre: str = Form(""), con=Depends(get_db)):
        groceries.clear_checked(con)
        return back_to_groceries(request, filtre)

    @app.get("/epicerie/{item_id:int}")
    def grocery_page(request: Request, item_id: int, filtre: str = "", con=Depends(get_db)):
        item = groceries.get_item(con, item_id)
        if item is None:
            return RedirectResponse(groceries_url(filtre), status_code=303)
        return item_page(request, con, item, filtre)

    @app.post("/epicerie/{item_id:int}/modifier")
    def edit_grocery(
        request: Request,
        item_id: int,
        nom: str = Form(""),
        quantite: str = Form(""),
        etiquettes: str = Form(""),
        rayon: int = Form(0),
        filtre: str = Form(""),
        con=Depends(get_db),
    ):
        item = groceries.get_item(con, item_id)
        if item is None:
            return back_to_groceries(request, filtre)
        tags = [t.strip() for t in etiquettes.split(",") if t.strip()]
        # Le formulaire renvoie toujours un rayon : seul un changement doit être retenu.
        aisle_id = rayon if rayon and rayon != item.aisle_id else None
        try:
            groceries.update_item(con, item_id, nom, quantite, tags, aisle_id)
        except ValueError as error:
            draft = replace(item, name=nom, quantity=quantite, tags=tags, aisle_id=rayon)
            return item_page(request, con, draft, filtre, str(error))
        return back_to_groceries(request, filtre)

    @app.get("/epicerie/stock")
    def staples_page(request: Request, con=Depends(get_db)):
        return render(
            request, "stock.html", {"section": "epicerie", "staples": groceries.list_staples(con)}
        )

    @app.post("/epicerie/stock/ajouter")
    def staples_add(request: Request, nom: str = Form(""), con=Depends(get_db)):
        groceries.add_staple(con, nom, request.state.person)
        return RedirectResponse("/epicerie/stock", status_code=303)

    @app.post("/epicerie/stock/retirer")
    def staples_remove(nom: str = Form(""), con=Depends(get_db)):
        groceries.remove_staple(con, nom)
        return RedirectResponse("/epicerie/stock", status_code=303)

    @app.get("/recettes")
    def recipes_page(request: Request, con=Depends(get_db)):
        return render(
            request, "recettes.html", {"section": "recettes", "recipes": recipes.list_recipes(con)}
        )

    @app.get("/recettes/{recipe_id:int}")
    def recipe_page(request: Request, recipe_id: int, con=Depends(get_db)):
        recipe = recipes.get(con, recipe_id)
        if recipe is None:
            return RedirectResponse("/recettes", status_code=303)
        return render(request, "recette.html", {"section": "recettes", "recipe": recipe})

    @app.post("/recettes/{recipe_id:int}/supprimer")
    def recipe_delete(recipe_id: int, con=Depends(get_db)):
        recipes.delete(con, recipe_id)
        return RedirectResponse("/recettes", status_code=303)

    def task_filter(request: Request, pour: str) -> tuple[str, str | None]:
        """Le filtre « pour » tel que les puces le nomment, et la personne qu'il garde."""
        me = request.state.person
        if pour == "moi":
            return "moi", me
        if pour == "personne":
            return "personne", ""
        key = groceries.normalize(pour)
        name = next((n for n in names if key and groceries.normalize(n) == key), None)
        if name is None:
            return "", None
        return ("moi" if name == me else name), name

    def tasks_url(colonne: str, pour: str) -> str:
        return with_query("/taches", colonne=colonne, pour=pour)

    def tasks_context(request: Request, con, colonne: str, pour: str) -> dict:
        column = tasks.column_for(colonne)
        pour, assignee = task_filter(request, pour)
        me = request.state.person
        return {
            "section": "taches",
            "column": column,
            "columns": tasks.COLUMNS,
            "board": tasks.board(con, assignee),
            "pour": pour,
            "filters": [
                ("", "Tout le monde"),
                ("moi", "Moi"),
                *[(n, n) for n in names if n != me],
                ("personne", "Personne"),
            ],
            "colors": {n: f"who-{i}" for i, n in enumerate(names)},
            "query": urlencode({"colonne": column.slug, "pour": pour}),
            "etag": dated_etag(con),
        }

    def task_page(
        request: Request,
        draft: tasks.Fields,
        colonne: str,
        pour: str,
        task: tasks.Task | None = None,
        error: str = "",
    ) -> Response:
        return render(
            request,
            "tache.html",
            {
                "section": "taches",
                "task": task,
                "draft": draft,
                "names": names,
                "columns": tasks.COLUMNS,
                "priorities": tasks.PRIORITY_LABELS,
                "colonne": colonne,
                "pour": pour,
                "back": tasks_url(colonne, pour),
                "error": error,
            },
            status_code=422 if error else 200,
        )

    def task_form(titre: str, priorite: int, personne: str, etat: str, note: str):
        # L'échéance est lue à part : une date invalide doit pouvoir réafficher la fiche.
        return tasks.Fields(
            title=titre,
            note=note,
            status=tasks.column_for(etat).status,
            priority=priorite,
            assignee=personne,
        )

    @app.get("/taches")
    def tasks_page(request: Request, colonne: str = "", pour: str = "", con=Depends(get_db)):
        return render(request, "taches.html", tasks_context(request, con, colonne, pour))

    @app.get("/taches/liste")
    def tasks_fragment(request: Request, colonne: str = "", pour: str = "", con=Depends(get_db)):
        return unless_unchanged(
            request,
            dated_etag(con),
            lambda: render(
                request, "_taches_liste.html", tasks_context(request, con, colonne, pour)
            ),
        )

    @app.get("/taches/nouvelle")
    def new_task_page(request: Request, colonne: str = "", pour: str = ""):
        me = request.state.person
        draft = tasks.Fields(
            title="",
            status=tasks.column_for(colonne).status,
            assignee=me if me in names else "",
        )
        return task_page(request, draft, colonne, pour)

    @app.post("/taches/ajouter")
    def add_task(
        request: Request,
        titre: str = Form(""),
        priorite: int = Form(tasks.NORMAL),
        personne: str = Form(""),
        etat: str = Form(""),
        echeance: str = Form(""),
        note: str = Form(""),
        colonne: str = Form(""),
        pour: str = Form(""),
        con=Depends(get_db),
    ):
        draft = task_form(titre, priorite, personne, etat, note)
        try:
            draft.due = tasks.parse_due(echeance)
            tasks.add(con, [draft], request.state.person, names)
        except ValueError as error:
            return task_page(request, draft, colonne, pour, error=str(error))
        # Vers la colonne de la nouvelle tâche, pour qu'on la voie.
        return RedirectResponse(tasks_url(tasks.column_for(etat).slug, pour), status_code=303)

    @app.get("/taches/{task_id:int}")
    def task_sheet(
        request: Request, task_id: int, colonne: str = "", pour: str = "", con=Depends(get_db)
    ):
        task = tasks.get(con, task_id)
        if task is None:
            return RedirectResponse(tasks_url(colonne, pour), status_code=303)
        return task_page(request, task, colonne, pour, task)

    @app.post("/taches/{task_id:int}/modifier")
    def edit_task(
        request: Request,
        task_id: int,
        titre: str = Form(""),
        priorite: int = Form(tasks.NORMAL),
        personne: str = Form(""),
        etat: str = Form(""),
        echeance: str = Form(""),
        note: str = Form(""),
        colonne: str = Form(""),
        pour: str = Form(""),
        con=Depends(get_db),
    ):
        task = tasks.get(con, task_id)
        if task is None:
            return RedirectResponse(tasks_url(colonne, pour), status_code=303)
        draft = task_form(titre, priorite, personne, etat, note)
        try:
            draft.due = tasks.parse_due(echeance)
            tasks.update(con, task_id, draft, names)
        except ValueError as error:
            return task_page(request, draft, colonne, pour, task, str(error))
        return RedirectResponse(tasks_url(colonne, pour), status_code=303)

    @app.post("/taches/{task_id:int}/avancer")
    def advance_task(
        request: Request,
        task_id: int,
        de: str = Form(""),
        colonne: str = Form(""),
        pour: str = Form(""),
        con=Depends(get_db),
    ):
        tasks.advance(con, task_id, de)
        return back(request, tasks_url(colonne, pour))

    @app.post("/taches/{task_id:int}/supprimer")
    def remove_task(
        task_id: int, colonne: str = Form(""), pour: str = Form(""), con=Depends(get_db)
    ):
        tasks.remove(con, task_id)
        return RedirectResponse(tasks_url(colonne, pour), status_code=303)

    def purchases_url(statut: str, etiquette: str) -> str:
        return with_query("/achats", statut=statut, etiquette=etiquette)

    def purchases_context(con, statut: str, etiquette: str) -> dict:
        status = purchases.status_for(statut)
        tags = purchases.tag_names(con)
        key = groceries.normalize(etiquette)
        etiquette = next((t for t in tags if groceries.normalize(t) == key), etiquette)
        found = purchases.list_purchases(con, etiquette)
        bought = [p for p in found if p.bought]
        totals = purchases.totals(found)
        if status:
            shown, recent = [p for p in found if p.status == status.value], []
        else:
            shown, recent = (
                [p for p in found if not p.bought],
                [p for p in bought if p.recently_bought],
            )
        return {
            "section": "achats",
            "status": status,
            "statut": status.slug if status else "",
            "etiquette": etiquette,
            "statuses": purchases.STATUSES,
            "shown": shown,
            "recent": recent,
            "open_count": len(found) - len(bought),
            "bought_count": len(bought),
            "totals": [
                (label, totals[value], value == "ready") for value, label in purchases.TOTALS
            ],
            "tags": tags,
            "query": urlencode({"statut": status.slug if status else "", "etiquette": etiquette}),
            "etag": dated_etag(con),
        }

    def purchase_page(
        request: Request,
        draft: purchases.Fields,
        statut: str,
        etiquette: str,
        purchase: purchases.Purchase | None = None,
        error: str = "",
        typed: dict[str, str] | None = None,
    ) -> Response:
        """`typed` : les montants tels que tapés, pour réafficher une fiche invalide."""
        return render(
            request,
            "achat.html",
            {
                "section": "achats",
                "purchase": purchase,
                "draft": draft,
                "typed": typed or {},
                "statuses": purchases.STATUSES,
                "priorities": tasks.PRIORITY_LABELS,
                "statut": statut,
                "etiquette": etiquette,
                "back": purchases_url(statut, etiquette),
                "error": error,
            },
            status_code=422 if error else 200,
        )

    def purchase_form(titre: str, etiquettes: str, priorite: int, etat: str, details: str):
        # Les montants sont lus à part : un montant invalide doit pouvoir réafficher la fiche.
        return purchases.Fields(
            title=titre,
            details=details,
            status=(purchases.status_for(etat) or purchases.STATUSES[0]).value,
            priority=priorite,
            tags=[t.strip() for t in etiquettes.split(",") if t.strip()],
        )

    @app.get("/achats")
    def purchases_page(
        request: Request, statut: str = "", etiquette: str = "", con=Depends(get_db)
    ):
        return render(request, "achats.html", purchases_context(con, statut, etiquette))

    @app.get("/achats/liste")
    def purchases_fragment(
        request: Request, statut: str = "", etiquette: str = "", con=Depends(get_db)
    ):
        return unless_unchanged(
            request,
            dated_etag(con),
            lambda: render(
                request, "_achats_liste.html", purchases_context(con, statut, etiquette)
            ),
        )

    @app.get("/achats/nouveau")
    def new_purchase_page(request: Request, statut: str = "", etiquette: str = ""):
        draft = purchases.Fields(
            title="",
            status=(purchases.status_for(statut) or purchases.STATUSES[0]).value,
            tags=[etiquette] if etiquette.strip() else [],
        )
        return purchase_page(request, draft, statut, etiquette)

    @app.post("/achats/ajouter")
    def add_purchase(
        request: Request,
        titre: str = Form(""),
        etiquettes: str = Form(""),
        priorite: int = Form(tasks.NORMAL),
        etat: str = Form(""),
        budget: str = Form(""),
        paye: str = Form(""),
        details: str = Form(""),
        statut: str = Form(""),
        etiquette: str = Form(""),
        con=Depends(get_db),
    ):
        draft = purchase_form(titre, etiquettes, priorite, etat, details)
        try:
            draft.budget = purchases.parse_amount(budget)
            draft.paid = purchases.parse_amount(paye)
            purchases.add(con, [draft], request.state.person)
        except ValueError as error:
            typed = {"budget": budget, "paye": paye}
            return purchase_page(request, draft, statut, etiquette, None, str(error), typed)
        # Vers une vue où le nouvel achat se voit, en gardant le filtre si possible.
        filtered = purchases.status_for(statut)
        if filtered and filtered.value != draft.status:
            statut = purchases.status_named(draft.status).slug
        if groceries.normalize(etiquette) not in map(groceries.normalize, draft.tags):
            etiquette = ""
        return RedirectResponse(purchases_url(statut, etiquette), status_code=303)

    @app.get("/achats/{purchase_id:int}")
    def purchase_sheet(
        request: Request,
        purchase_id: int,
        statut: str = "",
        etiquette: str = "",
        con=Depends(get_db),
    ):
        purchase = purchases.get(con, purchase_id)
        if purchase is None:
            return RedirectResponse(purchases_url(statut, etiquette), status_code=303)
        return purchase_page(request, purchase, statut, etiquette, purchase)

    @app.post("/achats/{purchase_id:int}/modifier")
    def edit_purchase(
        request: Request,
        purchase_id: int,
        titre: str = Form(""),
        etiquettes: str = Form(""),
        priorite: int = Form(tasks.NORMAL),
        etat: str = Form(""),
        budget: str = Form(""),
        paye: str = Form(""),
        details: str = Form(""),
        statut: str = Form(""),
        etiquette: str = Form(""),
        con=Depends(get_db),
    ):
        purchase = purchases.get(con, purchase_id)
        if purchase is None:
            return RedirectResponse(purchases_url(statut, etiquette), status_code=303)
        draft = purchase_form(titre, etiquettes, priorite, etat, details)
        try:
            draft.budget = purchases.parse_amount(budget)
            draft.paid = purchases.parse_amount(paye)
            purchases.update(con, purchase_id, draft)
        except ValueError as error:
            typed = {"budget": budget, "paye": paye}
            return purchase_page(request, draft, statut, etiquette, purchase, str(error), typed)
        return RedirectResponse(purchases_url(statut, etiquette), status_code=303)

    @app.post("/achats/{purchase_id:int}/supprimer")
    def remove_purchase(
        purchase_id: int, statut: str = Form(""), etiquette: str = Form(""), con=Depends(get_db)
    ):
        purchases.remove(con, purchase_id)
        return RedirectResponse(purchases_url(statut, etiquette), status_code=303)

    return app
