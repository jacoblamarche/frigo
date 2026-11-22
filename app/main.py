from collections.abc import Iterator
from contextlib import asynccontextmanager, closing
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mcp.server.transport_security import TransportSecuritySettings

from app import groceries
from app.db import connect, data_version, migrate
from app.mcp_server import AUTHOR, build_mcp
from app.settings import Settings, bearer_ok, same_origin

APP_DIR = Path(__file__).parent
PUBLIC_PATHS = ("/healthz", "/static/")
MCP_PATH = "/mcp"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SECTIONS = [{"id": "epicerie", "label": "Épicerie", "path": "/epicerie"}]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db_path = settings.data_dir / "frigo.sqlite3"
    with closing(connect(db_path)) as con:
        migrate(con)

    mcp = build_mcp(db_path)
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

    def render(request: Request, template: str, context: dict) -> Response:
        return templates.TemplateResponse(
            request,
            template,
            {"person": request.state.person, "sections": SECTIONS, **context},
        )

    def grocery_context(con, tag: str) -> dict:
        return {
            "section": "epicerie",
            "tag": tag,
            "items": groceries.list_items(con, tag),
            "checked_items": groceries.list_items(con, checked=True),
            "tags": groceries.tag_counts(con),
            "total": len(groceries.list_items(con)),
            "untagged": len(groceries.list_items(con, "-")),
            "frequent": groceries.frequent(con),
            "version": data_version(con),
        }

    def back_to_groceries(request: Request, tag: str) -> Response:
        if request.headers.get("x-frigo-async"):
            return Response(status_code=204)
        query = f"?{urlencode({'etiquette': tag})}" if tag else ""
        return RedirectResponse(f"/epicerie{query}", status_code=303)

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
        etag = f'"{data_version(con)}"'
        headers = {"ETag": etag, "Cache-Control": "no-store"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        response = render(request, "_epicerie_liste.html", grocery_context(con, etiquette))
        response.headers.update(headers)
        return response

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

    return app
